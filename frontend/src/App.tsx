import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, relativeTime, type Approval, type Tool } from './hub';
import { attachDrag, attachResize, initWindow, windowAction } from './winbar';
import { Btn, CopyBlock, Ico, ICONS, Modal, Note, Prompt, Spinner, ToastHost, useAsync, useToast } from './ui';
import Dashboard from './views/dashboard';
import ToolsView from './views/tools';
import WorkflowsView from './views/workflows';
import RunsView from './views/runs';
import ConnectView from './views/connect';
import SettingsView from './views/settings';

type ViewId = 'dashboard' | 'tools' | 'workflows' | 'runs' | 'connect' | 'settings';

const VIEWS: { id: ViewId; label: string; path: string; hint: string; glyph: string }[] = [
  { id: 'dashboard', label: '总览', path: 'status', hint: '本机能力概况与下一步该做什么', glyph: '▸' },
  { id: 'tools', label: 'tools/', path: 'tools', hint: '扫描到的全部可调用工具', glyph: '▾' },
  { id: 'workflows', label: 'workflows/', path: 'workflows', hint: '把工具串成可复用的 DAG', glyph: '▾' },
  { id: 'runs', label: 'runs/', path: 'runs', hint: '每次调用的输入输出与产物', glyph: '▾' },
  { id: 'connect', label: 'agent/', path: 'agent', hint: 'REST / MCP / function calling 接入', glyph: '⇄' },
  { id: 'settings', label: 'settings', path: 'settings', hint: '扫描范围与风险开关', glyph: '⚙' },
];

function TreeNav({ view, counts, pending, onPick }: {
  view: ViewId; counts: Record<string, number | string>; pending: number; onPick: (v: ViewId) => void;
}) {
  return (
    <nav className="tree" aria-label="资源目录">
      <div className="cap">REGISTRY</div>
      <pre><span className="dir">workflowhub/</span></pre>
      {VIEWS.map((item, i) => (
        <button key={item.id} className="node" title={item.hint} onClick={() => onPick(item.id)}>
          {i === VIEWS.length - 1 ? '└─ ' : '├─ '}{item.glyph}{' '}
          <span className={view === item.id ? 'sel' : undefined}>{item.label}</span>
          {counts[item.id === 'dashboard' ? 'total' : item.id] !== undefined && (
            <span className="num">  {String(counts[item.id === 'dashboard' ? 'total' : item.id])}</span>
          )}
          {item.id === 'connect' && pending > 0 && <span className="ok">  ⚑{pending}</span>}
        </button>
      ))}
      <div className="cap" style={{ marginTop: 16 }}>EXECUTOR</div>
      <pre>
        <span className="dir">command</span>  <span className="ok">ready</span>{'\n'}
        <span className="dir">python</span>    <span className="ok">ready</span>{'\n'}
        {'   timeout  '}{counts.timeout ? `${counts.timeout}s` : '—'}{'\n'}
        {'   approval '}<span className={counts.approval === 'on' ? 'ok' : 'num'}>{counts.approval === 'on' ? '开启' : '关闭'}</span>{'\n'}
        {'   sandbox   '}<span className="num">非安全沙箱</span>
      </pre>
      <div className="foot">
        <div className="row"><span>发现条目</span><span className="right"><b>{counts.total}</b></span></div>
        <div className="row"><span>可调用</span><span className="right"><b>{counts.enabled}</b></span></div>
        <div className="row"><span>手写 skill</span><span className="right"><b>{counts.skills}</b></span></div>
      </div>
    </nav>
  );
}

function Palette({ onPick, onClose }: { onPick: (view: ViewId, tool?: Tool) => void; onClose: () => void }) {
  const [query, setQuery] = useState('');
  const [rows, setRows] = useState<Tool[]>([]);
  const [index, setIndex] = useState(0);
  const boxRef = useRef<HTMLInputElement>(null);
  useEffect(() => boxRef.current?.focus(), []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!query.trim()) { setRows([]); return; }
      api.tools({ query, limit: 12 }).then((list) => setRows(list.items)).catch(() => setRows([]));
    }, 220);
    return () => window.clearTimeout(timer);
  }, [query]);
  const views = VIEWS.filter((v) => !query || v.label.includes(query) || v.path.includes(query.toLowerCase()));
  const flat = [...views.map((v) => ({ type: 'view' as const, id: v.id, label: v.label })),
    ...rows.map((t) => ({ type: 'tool' as const, id: t.id, label: t.title }))];
  return (
    <>
      <div className="modal-mask" style={{ background: 'rgba(24,47,32,.16)' }} onClick={onClose} />
      <div className="palette">
        <div className="head">
          <span className="pr">$</span>
          <input ref={boxRef} placeholder="搜索工具，或输入 工作流 / 设置 / agent …" value={query}
            onChange={(e) => { setQuery(e.target.value); setIndex(0); }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') onClose();
              if (e.key === 'ArrowDown') setIndex((i) => Math.min(flat.length - 1, i + 1));
              if (e.key === 'ArrowUp') setIndex((i) => Math.max(0, i - 1));
              if (e.key === 'Enter') {
                const pick = flat[index];
                if (!pick) return;
                if (pick.type === 'view') onPick(pick.id as ViewId);
                else onPick('tools', rows.find((r) => r.id === pick.id));
                onClose();
              }
            }} />
          <span className="muted" style={{ fontSize: 11 }}>↑↓ 选择 · ⏎ 打开 · esc 关闭</span>
        </div>
        <div className="list">
          {views.map((v) => (
            <button key={v.id} className={`opt ${flat[index]?.id === v.id ? 'sel' : ''}`}
              onMouseEnter={() => setIndex(flat.findIndex((f) => f.id === v.id))}
              onClick={() => { onPick(v.id); onClose(); }}>
              <span className="k">▸</span><span>{v.label}</span><span className="h">{v.hint}</span>
            </button>
          ))}
          {rows.map((t) => (
            <button key={t.id} className={`opt ${flat[index]?.id === t.id ? 'sel' : ''}`}
              onMouseEnter={() => setIndex(flat.findIndex((f) => f.id === t.id))}
              onClick={() => { onPick('tools', t); onClose(); }}>
              <span className="k">$</span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</span>
              <span className="h">{t.category} · {t.risk}</span>
            </button>
          ))}
          {flat.length === 0 && <div className="muted" style={{ padding: 10 }}>输入关键词开始，例如 ffmpeg、下载、pdf、npm</div>}
        </div>
      </div>
    </>
  );
}

function HelpModal({ onClose }: { onClose: () => void }) {
  const quick = useAsync(() => api.quickstart(), []);
  return (
    <Modal title="man workflowhub — 上手说明" onClose={onClose} footer={<Btn variant="ghost" onClick={onClose}>关闭</Btn>}>
      <div style={{ display: 'grid', gap: 14 }}>
        <Note>WorkflowHub 把这台电脑上的<b>可执行程序、系统命令、脚本、项目入口、已装应用</b>统一登记成带参数说明的工具，
          让 AI Agent 通过同一个接口直接调用；每次调用都留下可回看的运行记录与审计。</Note>
        <div className="grid3">
          <div className="box pad"><div className="h1">1 扫描</div>
            <div className="soft" style={{ fontSize: 12 }}>读取 PATH 与注册表，并遍历设置里的目录找脚本与项目入口。目录变了就 <b>Ctrl+R</b> 重扫。</div></div>
          <div className="box pad"><div className="h1">2 划边界</div>
            <div className="soft" style={{ fontSize: 12 }}>星标常用工具；高风险工具（python / powershell / curl）可整体关闭，或开「审批模式」逐个放行。</div></div>
          <div className="box pad"><div className="h1">3 接 Agent</div>
            <div className="soft" style={{ fontSize: 12 }}>Agent 接入页复制 MCP 配置或 curl 片段；先用 <b>dry_run</b> 预览命令行，再正式执行。</div></div>
        </div>
        <div className="box pad">
          <div className="h1" style={{ marginBottom: 6 }}>界面每块的意义</div>
          <div className="soft" style={{ fontSize: 12, display: 'grid', gap: 4 }}>
            <span><b>顶部窗口栏</b>：整条可拖动，双击最大化；右侧 ─ / □ / ✕ 是窗口按钮，画在软件里，没有系统边框。</span>
            <span><b>左侧目录树</b>：六个页面就是六个目录节点，节点后的数字是实时条目数；下方 EXECUTOR 是执行器与开关现状。</span>
            <span><b>右侧输出区</b>：以 <b>$ 命令</b> 组织，每个区块标题就是它回答的问题（ls / tail / describe / man）。</span>
            <span><b>工具详情（man 页）</b>：它是什么、怎么调、参数表、试运行、调用片段、最近审计，一屏看完不用跳走。</span>
            <span><b>底部状态行</b>：服务是否正常、可用工具数、上次扫描、待批准数量。</span>
            <span><b>快捷键</b>：Ctrl+K 搜索跳转 · Ctrl+R 重扫 · 1…6 切页 · / 聚焦搜索 · Esc 关弹层。</span>
          </div>
        </div>
        <CopyBlock label="cat QUICKSTART（可复制给同事）" text={quick.data || quick.error || '读取中…'} height={130} />
      </div>
    </Modal>
  );
}

function ApprovalsModal({ items, onClose, onDecide }: {
  items: Approval[]; onClose: () => void; onDecide: (id: string, approve: boolean) => void;
}) {
  return (
    <Modal title={`待批准的工具调用 · ${items.length}`} onClose={onClose}>
      {items.length === 0 ? <Note>目前没有等待批准的调用。开启「审批模式」后，高风险工具会在这里等你放行。</Note> : (
        <div style={{ display: 'grid', gap: 10 }}>
          {items.map((a) => (
            <div className="box pad" key={a.id}>
              <div className="row">
                <b>{a.title}</b><span className="pill bad">{a.risk}</span>
                <span className="right muted">{relativeTime(a.created_at)}</span>
              </div>
              <pre className="code" style={{ maxHeight: 120, marginTop: 6 }}>{JSON.stringify(a.payload, null, 2)}</pre>
              <div className="row right">
                <Btn variant="danger" onClick={() => onDecide(a.id, false)}>✗ 拒绝</Btn>
                <Btn variant="primary" onClick={() => onDecide(a.id, true)}>✓ 批准执行</Btn>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function Shell() {
  const [view, setView] = useState<ViewId>(() => {
    const hash = window.location.hash.replace(/^#\//, '') as ViewId;
    return VIEWS.some((v) => v.id === hash) ? hash : ((localStorage.getItem('hub.view') as ViewId) || 'dashboard');
  });
  const [desktop, setDesktop] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem('hub.theme') || 'light');
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const [approvals, setApprovals] = useState(false);
  const [pending, setPending] = useState<Approval[]>([]);
  const [focusTool, setFocusTool] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const toast = useToast();
  const overview = useAsync(() => api.overview(), []);
  const reloadOverview = useCallback(() => overview.reload(), [overview]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme === 'dark' ? 'ink' : 'light';
    localStorage.setItem('hub.theme', theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem('hub.view', view);
    if (window.location.hash !== `#/${view}`) window.history.replaceState(null, '', `#/${view}`);
  }, [view]);

  useEffect(() => {
    const onHash = () => {
      const hash = window.location.hash.replace(/^#\//, '') as ViewId;
      if (VIEWS.some((v) => v.id === hash)) setView(hash);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    void initWindow().then((mode) => {
      setDesktop(mode === 'desktop');
      void api.windowState().then((info) => setMaximized(!!info.state?.maximized)).catch(() => undefined);
    });
  }, []);

  useEffect(() => {
    const el = barRef.current;
    if (!el) return undefined;
    return attachDrag(el, (moving) => {
      if (!moving) void api.windowState().then((info) => setMaximized(!!info.state?.maximized)).catch(() => undefined);
    });
  }, [desktop]);

  useEffect(() => {
    const bind = (selector: string, edges: string) => {
      document.querySelectorAll<HTMLElement>(selector).forEach((node) => attachResize(node, edges));
    };
    bind('.gz.n', 't'); bind('.gz.s', 'b'); bind('.gz.w', 'l'); bind('.gz.e', 'r');
    bind('.gz.nw', 'lt'); bind('.gz.ne', 'rt'); bind('.gz.sw', 'lb'); bind('.gz.se', 'rb');
  }, []);

  useEffect(() => {
    const tick = window.setInterval(() => {
      void api.approvals().then((rows) => setPending(rows.pending)).catch(() => undefined);
    }, 5000);
    void api.approvals().then((rows) => setPending(rows.pending)).catch(() => undefined);
    return () => window.clearInterval(tick);
  }, []);

  const rescan = useCallback(async () => {
    toast('正在扫描本机…');
    const until = Date.now() + 90_000;
    await api.scan(null, false);
    const poll = window.setInterval(() => {
      void api.scanStatus().then((status) => {
        if (!status.scan_state.running || Date.now() > until) {
          window.clearInterval(poll);
          reloadOverview();
          toast(`扫描完成：${status.stats.total ?? '—'} 条`, 'ok');
          setView('tools');
        }
      }).catch(() => window.clearInterval(poll));
    }, 800);
  }, [reloadOverview, toast]);

  const decide = useCallback(async (id: string, approve: boolean) => {
    await api.decide(id, approve);
    toast(approve ? '已批准，Agent 可重试' : '已拒绝', approve ? 'ok' : 'bad');
    setPending((prev) => prev.filter((p) => p.id !== id));
  }, [toast]);

  const counts = useMemo(() => {
    const data = overview.data;
    return {
      total: data?.catalog.total ?? 0, enabled: data?.catalog.enabled ?? 0,
      tools: data?.catalog.enabled ?? 0, workflows: data?.workflows.count ?? 0,
      runs: data?.recent_runs.length ?? 0, skills: data?.skills.count ?? 0,
      timeout: Number(data?.settings?.default_timeout_seconds ?? 120),
      approval: data?.settings?.approval_mode ? 'on' : 'off',
    } as Record<string, number | string>;
  }, [overview.data]);

  const path = VIEWS.find((v) => v.id === view)?.path ?? '';
  const status = overview.error ? { text: '无响应', tone: 'bad' }
    : overview.busy ? { text: '读取中', tone: '' } : { text: 'ready', tone: 'ok' };

  return (
    <div className="term" data-desktop={desktop ? '1' : '0'} data-maximized={maximized ? '1' : '0'}>
      <header className="bar">
        <div className="drag" ref={barRef} onDoubleClick={() => void windowAction('toggle-maximize')}>
          <span className="dot g" /><span className="dot" /><span className="dot" />
          <span className="addr">workflowhub://<b>{path}</b></span>
          <span className="quick">
            <button title="搜索工具或跳转（Ctrl+K）" onClick={() => setPalette(true)}>⌕ 搜索</button>
            <button title="帮助与上手指南" onClick={() => setHelp(true)}>man</button>
            <button title="重新扫描本机（Ctrl+R）" onClick={() => void rescan()}>↻ 扫描</button>
            <button title={theme === 'light' ? '切到夜间终端' : '切回纸面终端'}
              onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? '◐ 夜间' : '◑ 纸面'}</button>
            {pending.length > 0 && (
              <button title="有工具调用等待你批准" onClick={() => setApprovals(true)} style={{ color: 'var(--amber)' }}>⚑ {pending.length} 待批</button>
            )}
            {!desktop && <span className="pill req" title="在浏览器里预览：窗口按钮不控制桌面窗口">浏览器预览</span>}
          </span>
        </div>
        <span className="tab">{view === 'tools' ? 'ls — tools' : view === 'runs' ? 'tail — runs' : 'run — bash'}</span>
        <div className="wbtns">
          <button className="wbtn" title="最小化" onClick={() => void act('minimize', toast, desktop)}><Ico d={ICONS.min} /></button>
          <button className="wbtn" title={maximized ? '还原窗口（双击标题栏也可）' : '最大化窗口'} onClick={() => void windowAction('toggle-maximize')}>
            <Ico d={maximized ? ICONS.restore : ICONS.max} size={11} />
          </button>
          <button className="wbtn close" title="退出 WorkflowHub" onClick={() => void act('close', toast, desktop)}><Ico d={ICONS.close} /></button>
        </div>
      </header>

      <div className="layout">
        <TreeNav view={view} counts={counts} pending={pending.length} onPick={setView} />
        <main className="out">
          {overview.error && <Note tone="bad">本地服务没有响应：{overview.error}。刚启动请等两秒后按 Ctrl+R，或点右上「↻ 扫描」。</Note>}
          {view === 'dashboard' && (
            <Dashboard overview={overview.data} busy={overview.busy} error={overview.error} onReload={reloadOverview}
              onRescan={() => void rescan()} onOpen={() => setView('tools')} go={setView} />
          )}
          {view === 'tools' && <ToolsView focus={focusTool} onFocusDone={() => setFocusTool(null)} onChanged={reloadOverview} />}
          {view === 'workflows' && <WorkflowsView onChanged={reloadOverview} />}
          {view === 'runs' && <RunsView onChanged={reloadOverview} />}
          {view === 'connect' && <ConnectView />}
          {view === 'settings' && <SettingsView overview={overview.data} onReload={reloadOverview} onRescan={() => void rescan()} />}
          <Prompt note={<span className="muted">Ctrl+K 搜索 · 1…6 切页 · 悬停任何标记看解释</span>}>
            <span className="muted">workflowhub {path} <span className="fl">--help</span></span>
          </Prompt>
        </main>
      </div>

      <footer className="foot">
        <span><b>status</b> <span className={`mark ${status.tone}`}>{status.text}</span></span>
        <span><b>tools</b> {counts.enabled}/{counts.total} 可调用</span>
        <span><b>scanned</b> {overview.data?.last_scan?.at ? relativeTime(String(overview.data.last_scan.at)) : '从未'}</span>
        {overview.data?.scan_state?.running && <span className="mark warn">扫描中：{overview.data.scan_state.phase} · {overview.data.scan_state.found} 条</span>}
        {pending.length > 0 && <span className="mark warn">{pending.length} 个调用待批准</span>}
        <span className="sp" />
        <span><b>events</b> data/catalog/audit.jsonl</span>
        <span><b>ui</b> {overview.data?.service.static_ui ? '内嵌' : '未构建'}</span>
        <span><b>v</b>{overview.data?.service.version || '—'}</span>
      </footer>

      {desktop && ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'].map((edge) => <div key={edge} className={`gz ${edge}`} />)}
      {palette && <Palette onPick={(next, tool) => { setView(next); if (tool) setFocusTool(tool.id); }} onClose={() => setPalette(false)} />}
      {help && <HelpModal onClose={() => setHelp(false)} />}
      {approvals && <ApprovalsModal items={pending} onClose={() => setApprovals(false)} onDecide={(id, ok) => void decide(id, ok)} />}
      {overview.busy && !overview.data && (
        <div className="pulse"><span className="row"><Spinner label="读取本机目录…" /></span></div>
      )}
    </div>
  );
}

async function act(action: string, toast: (text: string, tone?: string) => void, desktop: boolean) {
  const result = await windowAction(action);
  if (!result.ok && !desktop) {
    toast(action === 'close' ? '浏览器模式：直接关闭标签页即可' : '浏览器模式无法控制窗口，请用桌面版', 'bad');
  }
}

export default function App() {
  return <ToastHost><Shell /></ToastHost>;
}
