import { clockTime, relativeTime, type Overview } from '../hub';
import { Bars, Box, Btn, Cursor, Dots, Empty, Fl, Log, LogLine, Note, Prompt, RiskPill, SecCap, StatusPill } from '../ui';

const NEXT_STEPS = [
  { key: 'scan', title: '扫描本机', desc: '把 PATH、注册表与目录里的脚本登记成工具', done: (o: Overview) => o.catalog.total > 0 },
  { key: 'try', title: '试运行一个工具', desc: '先 dry_run 看命令行，再真正执行', done: (o: Overview) => o.activity.some((a) => a.type === 'invoke') },
  { key: 'star', title: '收藏常用工具', desc: '星标会排在前面，Agent 搜索时权重更高', done: (o: Overview) => o.catalog.starred > 0 },
  { key: 'agent', title: '接给 Agent', desc: '复制 MCP 配置或 curl 片段，让 Agent 直接调用本机工具', done: (o: Overview) => o.activity.some((a) => a.type === 'invoke' && (a.actor || '') !== 'workflowhub-ui') },
  { key: 'flow', title: '串成工作流', desc: '把两三个工具连成 DAG，一次编排反复复用', done: (o: Overview) => o.workflows.count > 1 },
];

export default function Dashboard({ overview, busy, error, onReload, onRescan, onOpen, go }: {
  overview: Overview | null; busy: boolean; error: string | null;
  onReload: () => void; onRescan: () => void; onOpen: (key: string) => void;
  go: (view: 'dashboard' | 'tools' | 'workflows' | 'runs' | 'connect' | 'settings') => void;
}) {
  if (error) {
    return (
      <>
        <Prompt note={<span className="muted">读取本机目录</span>}>workflowhub status</Prompt>
        <Note tone="bad">{error}。刚启动请等两秒；仍不行点右上「↻ 扫描」或 <Btn small variant="ghost" onClick={onReload}>重试</Btn></Note>
      </>
    );
  }
  if (!overview) {
    return (
      <>
        <Prompt>workflowhub status</Prompt>
        <div className="grid4">{[0, 1, 2, 3].map((i) => <div key={i} className="skel" style={{ height: 66 }} />)}</div>
        <div className="skel" style={{ height: 120 }} />
      </>
    );
  }
  const { catalog } = overview;
  const steps = NEXT_STEPS.map((s) => ({ ...s, done: s.done(overview) }));
  const next = steps.find((s) => !s.done);
  const invoked = Object.entries(overview.activity.filter((a) => a.type === 'invoke' && a.tool)
    .reduce<Record<string, number>>((acc, a) => { acc[String(a.tool)] = (acc[String(a.tool)] || 0) + 1; return acc; }, {}))
    .sort((a, b) => b[1] - a[1]).slice(0, 8);

  return (
    <>
      <Prompt note={<span className="muted">{steps.filter((s) => s.done).length}/{steps.length} 步已完成</span>}>
        workflowhub status <Fl>--human</Fl>
      </Prompt>

      <div className="grid4">
        <Box>
          <div className="kv"><div className="v">{catalog.total}</div><div className="l">发现条目</div>
            <div className="h">可执行 / 脚本 / 应用 / 项目入口</div></div>
        </Box>
        <Box>
          <div className="kv"><div className="v">{catalog.enabled}</div><div className="l">Agent 可调用</div>
            <div className="h"><span style={{ cursor: 'pointer' }} onClick={() => go('tools')}>$ ls tools/ →</span></div></div>
        </Box>
        <Box>
          <div className="kv"><div className="v">{overview.skills.count}</div><div className="l">手写 skill 包</div>
            <div className="h">skills/ 里的固定契约</div></div>
        </Box>
        <Box>
          <div className="kv"><div className="v">{overview.workflows.count}</div><div className="l">已发布 workflow 版本</div>
            <div className="h">{overview.recent_runs.length ? `最近 ${overview.recent_runs.length} 次运行` : '还没有运行'}</div></div>
        </Box>
      </div>

      <div className="grid2" style={{ gridTemplateColumns: '1.35fr 1fr' }}>
        <section>
          <SecCap title="TODO --next" note="按顺序做完就上手了" />
          <Box>
            <table className="ls">
              <thead><tr><th style={{ width: 42 }}>状态</th><th>步骤</th><th className="nowrap">作用</th><th style={{ width: 68 }} /></tr></thead>
              <tbody>
                {steps.map((step) => (
                  <tr key={step.key} style={{ cursor: 'default' }}>
                    <td className="nowrap">{step.done ? <span className="pill free">[x] 完成</span> : <span className="pill dim">[ ] 待办</span>}</td>
                    <td className="nowrap"><b>{step.title}</b></td>
                    <td className="desc">{step.desc}</td>
                    <td>{!step.done && (
                      <Btn small onClick={() => {
                        if (step.key === 'scan') onRescan();
                        else if (step.key === 'try' || step.key === 'star') go('tools');
                        else if (step.key === 'agent') go('connect');
                        else go('workflows');
                      }}>去做</Btn>
                    )}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Box>
          {next && <div style={{ marginTop: 8 }}><Note>建议先做「{next.title}」：{next.desc}</Note></div>}
        </section>

        <section>
          <SecCap title="LS tools/ --group=category" note="点右侧数字进工具库" />
          <Box pad>
            <Bars data={catalog.categories} />
            <div className="row tight" style={{ marginTop: 10 }}>
              <RiskPill risk="low" /><span className="muted">{catalog.risks.low ?? 0}</span>
              <RiskPill risk="medium" /><span className="muted">{catalog.risks.medium ?? 0}</span>
              <RiskPill risk="high" /><span className="muted">{catalog.risks.high ?? 0}</span>
              <span className="right muted">审批模式 {overview.settings.approval_mode ? '开' : '关'}</span>
            </div>
          </Box>
        </section>
      </div>

      <section>
        <SecCap title="TAIL catalog/audit.jsonl" note="谁在什么时候调了什么" />
        {overview.activity.length === 0 ? (
          <Empty title="tail: 没有调用记录">在工具库里点「试运行」，或让 Agent 调一次；这里会出现时间线。<span style={{ cursor: 'pointer' }} className="fl" onClick={() => go('tools')}>打开工具库</span></Empty>
        ) : (
          <Log>
            {overview.activity.slice(0, 10).map((a, i) => (
              <LogLine key={`${a.at}-${i}`} ts={clockTime(a.at)} level={a.type === 'invoke' ? (a.status === 'succeeded' ? 'ok' : 'err') : 'info'}>
                <span className="muted">{a.actor || '—'}</span> <Dots n={Math.max(3, 18 - (a.actor || '').length)} />{' '}
                {a.type === 'invoke' ? 'invoke' : a.type === 'workflow_run' ? 'workflow' : a.type === 'approval_decided' ? '审批' : a.type}{' '}
                <Fl>{a.tool || '—'}</Fl>
                {a.exit_code !== undefined && a.exit_code !== null && <> exit={a.exit_code}</>}
                {a.duration_ms !== undefined && <span className="am"> {a.duration_ms}ms</span>}
                {a.command_line && <span className="muted"> · {String(a.command_line).slice(0, 110)}</span>}
              </LogLine>
            ))}
            <LogLine><span className="muted">→ {catalog.enabled} 条工具已就绪</span> <Cursor /></LogLine>
          </Log>
        )}
      </section>

      <div className="grid2">
        <section>
          <SecCap title="HIST --top-invoked" note="最近被调用最多的工具" />
          {invoked.length === 0 ? <Box pad><span className="muted">还没有数据；调用几次后这里会出现排行。</span></Box> : (
            <Box>
              <table className="ls">
                <thead><tr><th>KEY</th><th style={{ width: 62 }}>次数</th><th>最近</th></tr></thead>
                <tbody>
                  {invoked.map(([key, count]) => (
                    <tr key={key} onClick={() => onOpen(key)}>
                      <td className="nowrap"><Fl>{key}</Fl></td>
                      <td>×{count}</td>
                      <td className="desc nowrap">{relativeTime(overview.activity.find((a) => a.tool === key)?.at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Box>
          )}
        </section>
        <section>
          <SecCap title="TAIL runs/" note={`共 ${overview.recent_runs.length} 条近期`} right={<Btn small variant="ghost" onClick={() => go('runs')}>全部 →</Btn>} />
          {overview.recent_runs.length === 0 ? <Box pad><span className="muted">任何调用都会在这里留下完整记录。</span></Box> : (
            <Box>
              <table className="ls">
                <thead><tr><th>RUN</th><th>目标</th><th>状态</th></tr></thead>
                <tbody>
                  {overview.recent_runs.slice(0, 7).map((run) => (
                    <tr key={run.id} onClick={() => go('runs')}>
                      <td className="nowrap"><Fl>{run.id.slice(0, 8)}</Fl></td>
                      <td className="desc nowrap">{run.kind === 'single-tool' ? run.catalog_key || run.workflow_id : run.workflow_id}</td>
                      <td className="nowrap"><StatusPill status={run.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Box>
          )}
        </section>
      </div>

      <Note>边界：扫描只读目录与注册表、不执行任何东西；命令以 <b>exec</b> 启动、不经过 shell，所以参数不会被拼成注入语句。
        但 WorkflowHub 只能约束调用链，不能替你判断命令后果 —— 请只对可信 Agent 开放端口
        （{busy ? '正在刷新…' : `数据更新于 ${relativeTime(overview.generated_at)}`}）。</Note>
    </>
  );
}
