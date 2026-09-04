package application

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	xcontext "go-common/library/context"
	"go-common/library/net/trace"

	"agent-router/app/container/application/writechain"
	"agent-router/app/container/domain/sandbox"
	"agent-router/app/container/infrastructure/agent"
	"agent-router/app/container/infrastructure/filesystem"
	"agent-router/app/container/infrastructure/redis"
	"agent-router/app/container/infrastructure/vcs"
	"agent-router/ecode"
	"agent-router/pkg/agui"
	// log 走项目 wrapper：自动把 ctx 里的 biz/mid/session_id/run_id 打成
	// 结构化字段（见 pkg/bizlog）。别名保持 log，行内调用无需改动。
	log "agent-router/pkg/bizlog"
	"agent-router/pkg/promptcontext"
	"agent-router/pkg/safetychecker"
)

// === Redis Stream 协议 ===
//
// 每个 sessionID 一个 Stream，存储最近一次 Run 的所有 AG-UI 事件
// 新 Run 来时 DEL 旧 Stream（同 sessionID 只保留最近一次）
const (
	// streamKeyPrefix Redis Stream key 前缀
	// 完整 key: sandbox:session:events:{sessionId}
	streamKeyPrefix = "sandbox:session:events:"

	// streamLiveTTL Run 进行中的 TTL（防 container 异常退出泄漏）
	// 5min 兜底，单次 Run 不会跑这么久
	streamLiveTTL = 5 * time.Minute

	// streamFinishedTTL Run 结束后保留时长（让客户端断流后有窗口重连）
	streamFinishedTTL = 5 * time.Minute

	// httpClientUploadTimeout 回放事件整包上传 BOSS 的兜底超时
	// run 结束后异步上传，给足时间（大 tool_result 可能整包较大），仍加超时防卡死
	httpClientUploadTimeout = 3 * time.Minute
)

// streamKey 拼 sessionId 对应的 Redis Stream key
func streamKey(sessionID string) string {
	return streamKeyPrefix + sessionID
}

// errSafetyBlocked 安全模型检测不通过的 sentinel error，仅供 buildRunPersistEvent 识别
var errSafetyBlocked = errors.New("safety check blocked")

// safetyReply 安全检测不通过时展示给用户的固定文案
const safetyReply = "抱歉，这个话题我暂时无法提供相关信息，我们聊点别的吧。"

// SessionStreamKey 暴露 UI Stream key 拼接给 interfaces 层做同步 ResetStream
// 与内部 streamKey 共享同一份常量，避免散落多处导致拼错
func SessionStreamKey(sessionID string) string {
	return streamKey(sessionID)
}

// overlaySource 提供配置注入层的本地缓存目录（由 OverlayManager 实现）
// 抽成接口便于测试注入；nil 表示未启用 overlay（注入被跳过）
// 按 biz 取对应业务线的缓存目录（空 biz = askb）。
type overlaySource interface {
	LocalDir(biz string) string
}

// SandboxService 沙箱生命周期 + 执行编排
// 编排顺序：sandbox domain（借 OS user）→ filesystem（建 HOME）→ agent（跑 claude）
type SandboxService struct {
	heartbeat  *HeartbeatService
	snapshot   *SnapshotService
	overlay    overlaySource
	transcript *TranscriptService // 每轮对话事件上传 BOSS 供回放；nil 表示不上传
}

// NewSandboxService heartbeat 用于 IncrSession/DecrSession；snapshot 做销毁前归档；
// overlay 提供配置注入层缓存目录（可为 nil，表示不注入）；
// transcript 上传每轮对话事件供回放（可为 nil，表示不上传）
func NewSandboxService(hb *HeartbeatService, snap *SnapshotService, overlay overlaySource, transcript *TranscriptService) *SandboxService {
	return &SandboxService{heartbeat: hb, snapshot: snap, overlay: overlay, transcript: transcript}
}

// ExecResult 暴露给 grpc 层的执行结果（非流式调用方拿这个）
type ExecResult struct {
	Content      string
	NumTurns     int32
	TotalCostUSD float64
}

// EventSink 流式调用的事件接收器
// 由调用方传入，agent 每个事件回调一次
// 当前主要用法：写 Redis Stream
type EventSink func(agui.Event)

// Create 为 uid 创建沙箱：借 OS user + 准备 HOME
// 幂等：已有沙箱直接返回成功
//
// acting 仅在新建时生效（写入 entity.acting 决定 Snapshot 是否跳过 BOSS 上传）；
// 复用已有 entity 时该参数被忽略（详见 sandbox.Acquire 注释）。
// operator 运营 OA 账号（仅 askc 有值），用于 overlay 注入时替换 ${ASKC_OPERATOR} 占位符。
func (s *SandboxService) Create(ctx context.Context, uid sandbox.UID, acting bool, operator string) error {
	startedAt := time.Now()
	preExist := sandbox.Has(uid)

	e, err := sandbox.Acquire(ctx, uid, acting)
	if err != nil {
		// 按 ecode 分类计数,便于告警区分"忙/关闭/池满"
		var result string
		switch {
		case errors.Is(err, ecode.SandboxClosing):
			result = "closing"
		case errors.Is(err, ecode.OSUserPoolFull) || errors.Is(err, ecode.SandboxBusy):
			result = "exhausted"
		default:
			result = "error"
		}
		metricSandboxAcquire.Incr(result)
		recordAcquireDur(result, preExist, uid.Biz, startedAt)
		return err
	}
	metricSandboxAcquire.Incr("ok")

	if !preExist {
		if err := s.prepareWorkspace(ctx, uid, operator, e.OSUser.UID, e.OSUser.GID); err != nil {
			// 准备失败（恢复+全新都没成），把 OS user 还回池子（避免泄漏）
			sandbox.Release(uid)
			recordAcquireDur("error", preExist, uid.Biz, startedAt)
			return err
		}
		s.heartbeat.IncrSession()
	}

	recordAcquireDur("ok", preExist, uid.Biz, startedAt)
	log.Infoc(ctx, "sandbox ready: uid=%v os_user=%s", uid, e.OSUser.Name)
	return nil
}

// prepareWorkspace 准备 uid 的 HOME：优先从 BOSS 恢复，未命中则全新初始化
//
//	命中快照 → Restore（解包 HOME，含 .git 和 ~/.claude 历史，无需再 init）
//	无快照   → PrepareHome（复制 skel）+ vcs.Init（git init + 基线 commit）
//
// 恢复出错时降级为全新初始化，保证创建不被持久化故障阻塞。
// 路径按业务 uid 算（cwd 恒定，claude resume 不受 os_user 槽位漂移影响），
// 所有权 chown 给当前借到的 os_user（osUID/osGID）。
func (s *SandboxService) prepareWorkspace(ctx context.Context, uid sandbox.UID, operator string, osUID, osGID int) error {
	pathKey := uid.Key()
	restoreStart := time.Now()
	restored, rerr := s.snapshot.Restore(ctx, uid, osUID, osGID)
	// restore 是冷启动最可能的大头（BOSS 下载 + 解包）,单独结算。
	// miss 也要记：没命中时这段是纯网络往返,能看出 BOSS 本身是否在抖。
	recordPrepareStep("restore", restoreResultLabel(restored, rerr), uid.Biz, time.Since(restoreStart).Milliseconds())
	if rerr != nil {
		// 恢复失败：记 error 后降级走全新（不阻断创建）
		log.Errorc(ctx, "restore failed, fallback to fresh: uid=%v err=%v", uid, rerr)
		restored = false
	}
	if restored {
		log.Infoc(ctx, "sandbox restored from boss: uid=%v", uid)
	} else {
		// 全新初始化
		homeStart := time.Now()
		ferr := filesystem.PrepareHome(ctx, pathKey, osUID, osGID)
		recordPrepareStep("prepare_home", okFailLabel(ferr), uid.Biz, time.Since(homeStart).Milliseconds())
		if ferr != nil {
			return ferr
		}
		// 在 workspace 初始化 git，开始记录变更（git 不可用时内部 silent skip）
		vcsStart := time.Now()
		verr := vcs.Init(ctx, filesystem.WorkspaceDir(pathKey), osUID, osGID)
		recordPrepareStep("vcs_init", okFailLabel(verr), uid.Biz, time.Since(vcsStart).Milliseconds())
		if verr != nil {
			// git 只是变更记录，初始化失败不阻断创建，记 error 后继续
			log.Errorc(ctx, "vcs.Init failed (non-fatal): uid=%v err=%v", uid, verr)
		}
	}

	// 注入配置层（CLAUDE.md / skills / .mcp.json / settings.json）。
	// 全新和恢复两条路径都跑：保证拿到最新配置，并覆盖快照里的旧配置。
	// overlay 未启用 / 缓存未就绪 → srcDir 为空，ApplyOverlay 内部跳过。
	// 注入失败不阻断创建（沙箱仍可 chat，只是少了 skill），记 error 即可。
	// overlay 源按 biz 选（askb→release，askc→creator-ops 分支）；
	// placeholderUID 用 uid.ID（askb=mid）供 ${ASKB_USER_UID} 替换，
	// operator（askc）供 ${ASKC_OPERATOR} 替换。
	if s.overlay != nil {
		srcDir := s.overlay.LocalDir(uid.Biz)
		overlayStart := time.Now()
		oerr := filesystem.ApplyOverlay(ctx, srcDir, pathKey, int(uid.ID), operator, osUID, osGID)
		recordPrepareStep("overlay", okFailLabel(oerr), uid.Biz, time.Since(overlayStart).Milliseconds())
		if oerr != nil {
			log.Errorc(ctx, "ApplyOverlay failed (non-fatal): uid=%v err=%v", uid, oerr)
		}
	}
	return nil
}

// ExecParams 单次流式执行所需的业务字段
//
// 老调用方（grpc unary Execute）传 ExecParams{UID, SessionID, Content} 即可，
// runID/checkDate/requestID 缺省时不会发 durable RunPersistEvent（无 router 持久化路径）
type ExecParams struct {
	UID       sandbox.UID
	SessionID string
	Content   string

	// Operator 运营 OA 账号（仅 askc 有值）。作为 MCP JWT 的 sub 透传给 agent。
	Operator string

	// runID DB 自增 id 字符串化；为空 → 不发 RunPersistEvent
	RunID string

	// CheckDate YYYY-MM-DD（Asia/Shanghai），透传到 RunPersistEvent.checkDate
	// 为空时 router 侧会按 DB run.check_date 兜底（见 run_persist_processor）
	CheckDate string

	// RequestID 业务幂等 key；透传给 router 做去重诊断
	RequestID string

	// JSTools 本次 Run 启用的前端工具集,由 router 从 paladin 注册表解析后传下来
	// worker 收到后注册 SDK MCP 工具,handler 通过 loopback 拿结果
	JSTools []agent.JSTool

	// PromptContext 是本次 Run 可选的模型上下文参数；零值不生成 <context>。
	PromptContext promptcontext.Context
}

// ExecuteStream 流式执行 agent（先抢锁再跑）的对外入口
//
// 给老调用方（grpc unary Execute / 自动化测试）保留 — 同步抢 ExecLock，
// 抢不到立刻返回 ecode.SandboxBusy/SandboxClosing。
//
// HTTP 异步路径（/internal/agent/start）走 ExecuteStreamLocked + 外层显式锁管理，
// 因为同步预检需要在返回 200 前就拿到 busy/closing 错误码。
func (s *SandboxService) ExecuteStream(ctx context.Context, uid sandbox.UID, sessionID, content, runID string, extraSink EventSink) error {
	e, err := sandbox.Get(ctx, uid)
	if err != nil {
		log.Warnc(ctx, "execute uid=%v not found", uid)
		return err
	}

	ok, destroying := e.TryAcquireExec()
	if !ok {
		if destroying {
			log.Warnc(ctx, "execute rejected: uid=%v in destroying state", uid)
			return ecode.SandboxClosing
		}
		log.Warnc(ctx, "execute busy: uid=%v another execute in flight", uid)
		return ecode.SandboxBusy
	}
	defer e.ReleaseExec()

	// 旧路径：本入口同时负责 ResetStream + ExecuteStreamLocked
	// 新路径（HTTP /start）已经在同步阶段做了 ResetStream，这里跳过避免重复
	if conn := redis.Ins(); conn != nil {
		if rerr := agui.ResetStream(ctx, conn, streamKey(sessionID)); rerr != nil {
			log.Warnc(ctx, "reset stream: sid=%s err=%v", sessionID, rerr)
		}
	}

	return s.ExecuteStreamLocked(ctx, e, ExecParams{
		UID:       uid,
		SessionID: sessionID,
		Content:   content,
		RunID:     runID,
	}, extraSink)
}

// ExecuteStreamLocked 假定外层已经持有 e.execMu 的执行入口
//
// 调用约束（违反会 panic / data race）：
//   - 调用前必须 TryAcquireExec() ok=true
//   - 调用后由调用方保证 ReleaseExec
//   - sessionID 对应的 UI Stream 由调用方负责 ResetStream（同步阶段）
//
// 终态时按 §5.1 emitRunPersist：构造自包含 RunPersistEvent 写 router:run-events
func (s *SandboxService) ExecuteStreamLocked(ctx context.Context, e *sandbox.Entity, p ExecParams, extraSink EventSink) error {
	startedAt := time.Now()

	conn := redis.Ins()
	uiKey := streamKey(p.SessionID)

	// 回放采集开关：仅在 transcript 启用且有 runID 时收集本轮事件,避免无谓内存占用。
	// 采集由写链的 ReplayAccumulator 完成(挂链与否由 CollectReplay 控制),
	// run 结束后读 wc.Replay.Snapshot() 整包上传 BOSS。
	collectForRepl := s.transcript != nil && p.RunID != ""

	// prompt 前缀：把 resp_mode + 按客户端时区换算后的可信服务器当前时刻拼到用户输入前面。
	// LevelHigh 走假流式固定文案不进 Claude,注入无意义;只在真正调 agent.Run 的两个分支用。
	// 落 DB 的 user_msg 仍是 p.Content 原文(在 router.preExecuteTx 里已经写好),不受影响。
	promptWithContext := p.PromptContext.Render(time.Now()) + p.Content

	// 安全等级前置检测：三级（安全/可疑/高危）
	// 高危：不走 Claude，直接假流式固定文案
	// 可疑：走 Claude，Run 后对输出做二次检测，高危则发 message.retract 召回
	// 安全：正常流程
	inputLevel := CheckInputLevel(ctx, p.Content)

	// outputCheckOn 综合判断：输入可疑 AND 后检开关已打开
	outputCheckOn := inputLevel == safetychecker.LevelSuspicious && OutputCheckEnabled()

	// WriteChain: 整条链的状态都装在 Handler struct 字段里(ui.lastStreamID / acc.snapshot / metric.timing / replay.events)
	// onEvent 闭包只负责: 构造 hctx → Process → 打日志(读 ui.LastStreamID()) → extraSink
	// 终态构造 RunPersistEvent 时直接读 acc.Snapshot(); 回放整包读 replay.Snapshot()
	wc := writechain.New(writechain.Options{
		RedisConn:         conn,
		UIStreamKey:       uiKey,
		MetricSink:        writeChainMetricSink{biz: p.UID.Biz},
		RunStartedAt:      startedAt,
		CollectReplay:     collectForRepl,
		BufferRunFinished: outputCheckOn, // 开关关闭时不挂 RunFinishedBuffer，事件实时写 Redis
	})

	onEvent := func(ev agui.Event) {
		// 1. 过 WriteChain（XADD / metric / 回放采集 / 累积 / 未来的风险词检测）
		hctx := &agui.HandlerContext{
			Event:     ev,
			SessionID: p.SessionID,
			RunID:     p.RunID,
			UID:       p.UID.ID,
		}
		// 链内 fail-open: 单 Handler error 不打断,通过返回值带出最近一次 err
		if _, herr := wc.Chain.Process(ctx, hctx); herr != nil {
			log.Warnc(ctx, "writechain: uid=%v sessionId=%s type=%s err=%v",
				p.UID, p.SessionID, ev.EventType(), herr)
		}
		// chain 可能改写过 Event,后续日志/extraSink 用最新版本
		ev = hctx.Event

		// 2. 日志（带上 UIStreamHandler XADD 后的 stream id, 直接从 handler 字段读）
		logAGUIEvent(ctx, p, wc.UI.LastStreamID(), ev)

		// 3. 兼容老 grpc Execute 的同步 sink
		if extraSink != nil {
			extraSink(ev)
		}
	}

	// 三级安全分发：高危→假流式固定文案；可疑→走 Claude + 输出检测；安全→正常流程
	var (
		runErr       error
		runRiskLevel safetychecker.Level // 最终落库的风险等级，默认 LevelSafe(0)
		riskContent  string              // 高危拦截时保存的原始内容，仅内部审计
	)

	switch inputLevel {
	case safetychecker.LevelHigh:
		// 前置高危：假流式固定文案，不走 Claude
		log.Infoc(ctx, "input safety high, serving canned stream: uid=%v sid=%s runId=%s",
			p.UID, p.SessionID, p.RunID)
		metricRiskKeywordHit.Incr("_")
		runRiskLevel = safetychecker.LevelHigh
		runErr = emitCannedStream(ctx, onEvent, loadRiskConfig(), p)

	case safetychecker.LevelSuspicious:
		// 可疑：走 Claude，outputCheckOn=true 时 Run 后对输出做二次检测
		log.Infoc(ctx, "input safety suspicious, running claude: uid=%v sid=%s runId=%s outputCheckOn=%v",
			p.UID, p.SessionID, p.RunID, outputCheckOn)
		runRiskLevel = safetychecker.LevelSuspicious
		_, runErr = agent.Ins().Run(ctx, &agent.RunRequest{
			UID:          p.UID.ID,
			Biz:          p.UID.Biz,
			Operator:     p.Operator,
			SessionID:    p.SessionID,
			RunID:        p.RunID,
			Prompt:       promptWithContext,
			OSUserUID:    e.OSUser.UID,
			OSUserGID:    e.OSUser.GID,
			OSUserName:   e.OSUser.Name,
			HomeDir:      filesystem.HomeDir(p.UID.Key()),
			WorkspaceDir: filesystem.WorkspaceDir(p.UID.Key()),
			JSTools:      p.JSTools,
			OnEvent:      onEvent,
			OnStarted: func(cancel func()) {
				e.SetCancelExec(p.SessionID, cancel)
			},
		})
		if outputCheckOn {
			// 后检开启：RunFinishedBuffer 已挂链，Run 结束后做输出安全检测，再补发 RUN_FINISHED
			log.Infoc(ctx, "suspicious run finished, runErr=%v, checking output: uid=%v sid=%s runId=%s",
				runErr, p.UID, p.SessionID, p.RunID)
			if runErr == nil {
				snap := wc.Accumulator.Snapshot()
				output := strings.Join(snap.AssistantText, "")
				if strings.TrimSpace(output) != "" && CheckOutputLevel(ctx, output) == safetychecker.LevelHigh {
					log.Infoc(ctx, "output safety high, sending retract: uid=%v sid=%s runId=%s",
						p.UID, p.SessionID, p.RunID)
					onEvent(agui.NewMessageRetract(snap.AssistantMessageID, safetyReply))
					runRiskLevel = safetychecker.LevelHigh
					riskContent = output
					runErr = errSafetyBlocked
				}
				// 可疑但输出通过：runRiskLevel 保持 LevelSuspicious，正常落库
			} else {
				// agent.Run 出错（超时/取消等）：不发 retract，直接补发 RUN_FINISHED 避免前端 SSE 挂住
				log.Infoc(ctx, "suspicious run error, skipping output check: uid=%v sid=%s runId=%s err=%v",
					p.UID, p.SessionID, p.RunID, runErr)
			}
			// 所有路径都必须补发 RUN_FINISHED，否则前端 SSE 永久挂住
			wc.RunFinishedBuffer.Release(onEvent)
		}
		// outputCheckOn=false 时 RunFinishedBuffer 未挂链，事件已实时写 Redis，无需 Release

	default: // LevelSafe
		// 调 agent CLI（fork claude + setuid + prlimit + stream-json）
		_, runErr = agent.Ins().Run(ctx, &agent.RunRequest{
			UID:          p.UID.ID,
			Biz:          p.UID.Biz,
			Operator:     p.Operator,
			SessionID:    p.SessionID,
			RunID:        p.RunID,
			Prompt:       promptWithContext,
			OSUserUID:    e.OSUser.UID,
			OSUserGID:    e.OSUser.GID,
			OSUserName:   e.OSUser.Name,
			HomeDir:      filesystem.HomeDir(p.UID.Key()),
			WorkspaceDir: filesystem.WorkspaceDir(p.UID.Key()),
			JSTools:      p.JSTools,
			OnEvent:      onEvent,
			OnStarted: func(cancel func()) {
				e.SetCancelExec(p.SessionID, cancel)
			},
		})
	}

	// 不论成功失败都给 Stream 设个 TTL（重连窗口）
	if conn != nil {
		// 用 Detach：run 已进终态、上层 ctx 可能马上被取消，但这条 EXPIRE
		// 必须发出去（否则 UI Stream 永不过期）。同时保住 trace 与业务字段。
		if eerr := agui.ExtendStreamTTL(xcontext.Detach(ctx), conn, uiKey, streamFinishedTTL); eerr != nil {
			log.Warnc(ctx, "extend stream ttl: sid=%s err=%v", p.SessionID, eerr)
		}
	}

	// 执行结束后提交一次变更记录（每轮对话一个 commit）
	// git 不可用 / 无变更时内部跳过；失败只记 warn，不影响主流程
	// 路径用业务 uid（cwd 恒定），git 身份仍用 os_user
	wsDir := filesystem.WorkspaceDir(p.UID.Key())
	if changed, verr := vcs.Commit(ctx, wsDir, e.OSUser.UID, e.OSUser.GID,
		commitMsg(p.SessionID)); verr != nil {
		log.Errorc(ctx, "vcs.Commit failed (non-fatal): uid=%v session=%s err=%v", p.UID, p.SessionID, verr)
	} else if changed {
		log.Infoc(ctx, "vcs.Commit ok: uid=%v session=%s", p.UID, p.SessionID)
	}

	// 终态时 emit RunPersistEvent（spec §5.1 重试链）
	// runID 为空表示老调用方（grpc unary）—— 没有 router 后半事务路径，跳过
	if p.RunID != "" {
		ev := buildRunPersistEvent(ctx, p, startedAt, runErr, wc.Accumulator.Snapshot(), runRiskLevel, riskContent)
		// 业务 metrics：run 终态计数 + 耗时 + cost + turns
		recordRunMetrics(ev)
		emitRunPersist(ctx, ev)
	}

	// 本轮事件整包上传 BOSS 供回放（best-effort，异步，不阻塞返回）
	// 失败仅告警：回放数据缺失不影响在线对话与已落库的最终回复
	// 事件由写链 ReplayAccumulator 采集,这里读 Snapshot 整包传
	if collectForRepl {
		s.uploadTranscriptAsync(ctx, p, startedAt, wc.Replay.Snapshot())
	}

	if runErr != nil && !errors.Is(runErr, errSafetyBlocked) {
		log.Errorc(ctx, "agent run failed: uid=%v session=%s err=%v", p.UID, p.SessionID, runErr)
		return ecode.SandboxExecError
	}
	return nil
}

// uploadTranscriptAsync 把本轮事件整包上传 BOSS 供回放（异步 best-effort）
//
// 用 Detach 而非直接继承 ctx：run 已结束，原 ctx 可能很快被取消，
// 而上传要另起超时。Detach 剥掉取消信号同时保住 trace 与业务字段，
// 与 emitRunPersist 的兜底链同一套路。
// 整个 goroutine 带 panic recover，任何失败只记 warn，绝不影响主流程。
func (s *SandboxService) uploadTranscriptAsync(ctx context.Context, p ExecParams, startedAt time.Time, events []agui.Event) {
	finishedAt := time.Now()
	detached := xcontext.Detach(ctx)
	go func() {
		ctx, cancel := context.WithTimeout(detached, httpClientUploadTimeout)
		defer cancel()
		defer func() {
			if r := recover(); r != nil {
				log.Warnc(ctx, "transcript upload panic: uid=%v session=%s run=%s panic=%v",
					p.UID, p.SessionID, p.RunID, r)
			}
		}()
		if err := s.transcript.UploadRun(ctx, RunBundle{
			PathKey:    p.UID.Key(),
			Mid:        p.UID.ID,
			Operator:   p.UID.Operator,
			SessionID:  p.SessionID,
			RunID:      p.RunID,
			UserInput:  p.Content,
			StartedAt:  startedAt,
			FinishedAt: finishedAt,
			Events:     events,
		}); err != nil {
			log.Warnc(ctx, "transcript upload failed (non-fatal): uid=%v session=%s run=%s err=%v",
				p.UID, p.SessionID, p.RunID, err)
		}
	}()
}

// recordRunMetrics 在 emitRunPersist 之前打点 run 终态业务指标
//
// 复用 RunPersistEvent 已经构造好的 status / numTurns / totalCostUsd / durationMs,
// 不重新解析 RunFinished.Result
//
// cost 用 ×10000 整数化避免浮点 sum 累积误差；查询时 PromQL 中 /10000 还原
// 同时记 Histogram(cost_dist) 看分位数（哪种 run 最贵）
//
// go-common prom API 调用约定: Incr(label_value, ...) — 第一个参数即 labels[0] 的值
func recordRunMetrics(ev *agui.RunPersistEvent) {
	if ev == nil {
		return
	}
	status := ev.Status // success / failed / cancelled
	metricAgentRunCount.Incr(status)
	metricAgentRunDuration.WithLabelValues(status).Observe(float64(ev.DurationMS))
	metricAgentRunTurns.Add(status, int64(ev.NumTurns))
	if ev.TotalCostUSD > 0 {
		costX10K := int64(ev.TotalCostUSD * 10000)
		metricAgentRunCostX10K.Add("_", costX10K)
		metricAgentRunCostDist.WithLabelValues("_").Observe(float64(costX10K))
	}
	// 仅 failed/cancelled 打错误类型分布；success 没有 ErrorType
	if status != "success" {
		metricAgentRunFailure.Incr(classifyRunError(ev.ErrorType, ev.ErrorMessage))
	}
}

func logAGUIEvent(ctx context.Context, p ExecParams, streamID agui.StreamEntryID, ev agui.Event) {
	payload, err := json.Marshal(ev)
	if err != nil {
		log.Warnc(ctx, "agui event marshal failed: uid=%v sessionId=%s runID=%s type=%s streamID=%s err=%v",
			p.UID, p.SessionID, p.RunID, ev.EventType(), streamID, err)
		return
	}
	log.Infoc(ctx, "agui event: uid=%v sessionId=%s runID=%s type=%s streamID=%s payload=%s",
		p.UID, p.SessionID, p.RunID, ev.EventType(), streamID, string(payload))
}

// buildRunPersistEvent 终态构造 RunPersistEvent
//
// 状态判定（来自 RunAccumulator 在事件流尾部的快照）:
//   - snap.GotFinished=true              → success
//   - snap.GotError && code=="cancelled" → cancelled
//   - snap.GotError                      → failed
//   - 其他（runErr 非空但没拿到任何终态事件）→ failed（兜底）
//
// 没有终态事件 + 没有 runErr 是不可能的：claude.go 至少会发一种
func buildRunPersistEvent(ctx context.Context, p ExecParams, startedAt time.Time, runErr error,
	snap writechain.RunSnapshot, riskLevel safetychecker.Level, riskContent string,
) *agui.RunPersistEvent {
	finishedAt := time.Now()
	durMS := int32(finishedAt.Sub(startedAt) / time.Millisecond)

	ev := agui.NewRunPersist(
		agui.GenEventID(),
		p.RunID,
		p.UID.ID,
		p.SessionID,
		p.RequestID,
		"", // status 下面填
	)
	// biz/operator 透传：router 侧 RunPersistProcessor 据此把终态写到 askb / askc 表
	ev.Biz = p.UID.Biz
	ev.Operator = p.Operator
	// trace 透传：Redis Stream 跨进程，ctx 上的 trace 过不去，只能塞进事件体。
	// router 侧 StreamConsumer / RunReaper 消费时据此接回同一条 trace。
	if t, ok := trace.FromContext(ctx); ok {
		ev.TraceID = t.TraceID()
	}
	ev.CheckDate = p.CheckDate
	ev.StartedAt = startedAt.UnixMilli()
	ev.FinishedAt = finishedAt.UnixMilli()
	ev.DurationMS = durMS

	switch {
	case errors.Is(runErr, errSafetyBlocked):
		// 后置高危拦截：对外 status=success（内容=固定文案），风控字段在末尾统一填充
		ev.Status = "success"
		ev.Content = safetyReply
		ev.NumTurns = 1
		ev.TotalCostUSD = 0

	case snap.GotFinished:
		ev.Status = "success"
		ev.Content = joinDeltas(snap.AssistantText)
		// 从 RunFinished.Result 解出 numTurns / cost
		if len(snap.RunResultRaw) > 0 {
			var r agui.AgentRunResult
			if err := json.Unmarshal(snap.RunResultRaw, &r); err == nil {
				ev.NumTurns = int32(r.NumTurns)
				ev.TotalCostUSD = r.TotalCostUSD
			}
		}

	case snap.GotError && snap.RunErrCode == "cancelled":
		ev.Status = "cancelled"
		ev.ErrorType = snap.RunErrCode
		ev.ErrorMessage = snap.RunErrMsg

	case snap.GotError:
		ev.Status = "failed"
		ev.ErrorType = snap.RunErrCode
		ev.ErrorMessage = snap.RunErrMsg
		ev.ErrorCode = int32(ecode.SandboxExecError.Code())

	default:
		// 兜底：没有任何终态事件但 Run 退出了
		ev.Status = "failed"
		ev.ErrorCode = int32(ecode.SandboxExecError.Code())
		if runErr != nil {
			ev.ErrorMessage = runErr.Error()
		} else {
			ev.ErrorMessage = "agent exited without terminal event"
		}
	}
	// 风控字段：由外层传入的 riskLevel/riskContent 统一填充（所有路径）
	ev.RiskLevel = int32(riskLevel)
	if riskLevel == safetychecker.LevelHigh {
		ev.RiskContent = riskContent
	}
	return ev
}

// Execute 非流式执行：内部跑 ExecuteStream + 累积成 ExecResult
// 给老调用方（grpc unary RPC / 自动化测试）保留
func (s *SandboxService) Execute(ctx context.Context, uid sandbox.UID, sessionID, content string) (*ExecResult, error) {
	var (
		finalText []string
		numTurns  int32
		costUSD   float64
		runErr    *agui.RunErrorEvent
	)
	sink := func(ev agui.Event) {
		switch x := ev.(type) {
		case *agui.TextMessageContentEvent:
			finalText = append(finalText, x.Delta)
		case *agui.RunFinishedEvent:
			// 解析 result 字段拿 num_turns / total_cost_usd
			var r agui.AgentRunResult
			if len(x.Result) > 0 {
				_ = json.Unmarshal(x.Result, &r)
			}
			numTurns = int32(r.NumTurns)
			costUSD = r.TotalCostUSD
		case *agui.RunErrorEvent:
			runErr = x
		}
	}
	// runID 不需要持久化（非流式调用方不关心），传空让 agent 自动生成
	if err := s.ExecuteStream(ctx, uid, sessionID, content, "", sink); err != nil {
		return nil, err
	}
	if runErr != nil {
		log.Warnc(ctx, "execute ended with RunError: uid=%v code=%s msg=%s",
			uid, runErr.Code, runErr.Message)
		return nil, fmt.Errorf("agent error: %s", runErr.Message)
	}
	content2 := joinDeltas(finalText)
	return &ExecResult{
		Content:      content2,
		NumTurns:     numTurns,
		TotalCostUSD: costUSD,
	}, nil
}

// commitMsg 拼一次执行后的 commit message
// 带上 session 便于在 git log 里区分是哪个对话产生的变更
func commitMsg(sessionID string) string {
	if sessionID == "" {
		return "chat: execute"
	}
	return "chat: execute session=" + sessionID
}

// joinDeltas 把多个文本 delta 拼成最终内容
func joinDeltas(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	if len(parts) == 1 {
		return parts[0]
	}
	total := 0
	for _, p := range parts {
		total += len(p)
	}
	buf := make([]byte, 0, total)
	for _, p := range parts {
		buf = append(buf, p...)
	}
	return string(buf)
}

// Cancel 取消正在执行的 Run（用户主动停止）
// sessionID 用于 CAS 校验：只有当前在跑的 sessionId 匹配才真的 cancel
//
// 通过 entity.CancelExec 触发 ctx 取消 + SIGTERM 进程组,claude 退出后会 emit RunError(cancelled)
//
// 返回:
//   - true:  成功触发 cancel
//   - false: 没有 Run 在跑 / 在跑的是别的 sessionId（过期 cancel,幂等成功）
//
// 注意：取消是异步的,本方法立即返回 true,实际 claude 进程退出可能要 1-5s
func (s *SandboxService) Cancel(ctx context.Context, uid sandbox.UID, sessionID string) bool {
	e, err := sandbox.Get(ctx, uid)
	if err != nil {
		return false
	}
	cancelled, mismatch := e.CancelExec(sessionID)
	if mismatch {
		log.Warnc(ctx, "cancel sessionId mismatch (treated as idempotent ok): uid=%v req_sid=%s current_sid=%s",
			uid, sessionID, e.CurrentSessionID())
	}
	return cancelled
}

// Destroy 销毁 uid 沙箱：kill 进程 + rm HOME + 还 OS user
// 幂等：不存在 / 已在销毁的 uid 都返回成功
func (s *SandboxService) Destroy(ctx context.Context, uid sandbox.UID) error {
	e, err := sandbox.Get(ctx, uid)
	if errors.Is(err, ecode.SandboxNotFound) {
		log.Infoc(ctx, "destroy not found, idempotent ok: uid=%v", uid)
		return nil
	}
	if err != nil {
		return err
	}

	if !e.MarkDestroying() {
		log.Infoc(ctx, "destroy already in progress, idempotent: uid=%v", uid)
		return nil
	}
	log.Infoc(ctx, "destroy: uid=%v os_user=%s starting", uid, e.OSUser.Name)

	// 等当前 Execute 完成（不取消，让 claude 跑完，timeLimit 兜底）
	e.WaitExecDone()

	// 清理前打最终快照上传 BOSS（claude 已停，状态强一致）
	// 失败只告警不阻断清理：避免快照故障导致 os_user 泄漏
	if serr := s.snapshot.Snapshot(ctx, uid, e.OSUser.UID, e.OSUser.GID); serr != nil {
		log.Errorc(ctx, "destroy snapshot failed (non-fatal): uid=%v err=%v", uid, serr)
	}

	// 先杀残留进程（按 os_user 进程 owner），再清 HOME（按业务 uid 路径）
	filesystem.KillByUID(ctx, e.OSUser.UID)

	if cerr := filesystem.CleanHome(ctx, uid.Key()); cerr != nil {
		log.Errorc(ctx, "clean home failed: uid=%v os_user=%s err=%v", uid, e.OSUser.Name, cerr)
	}

	sandbox.Release(uid)
	s.heartbeat.DecrSession()

	log.Infoc(ctx, "sandbox destroyed: uid=%v os_user=%s", uid, e.OSUser.Name)
	return nil
}

// KillAll 终止所有活跃沙箱的在途 claude 进程（优雅关闭 drain 用）
// 不清 HOME、不还 os_user、不打快照——只是让 claude 停止写文件，
// 后续由 SnapshotAll 打快照。被 kill 的对话靠用户重试在新 Pod 恢复。
func (s *SandboxService) KillAll(ctx context.Context) {
	entities := sandbox.List()
	for _, e := range entities {
		filesystem.KillByUID(ctx, e.OSUser.UID)
	}
	log.Infoc(ctx, "drain kill all: %d sandboxes processes killed", len(entities))
}
