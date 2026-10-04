import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { KIND_LABELS, RISK_HINT, RISK_LABELS, relativeTime, type Risk } from './hub';

/* ── 字形与提示行 ─────────────────────────────────────────── */
export const Ico = ({ d, size = 14 }: { d: string; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={1.8} strokeLinecap="round" aria-hidden style={{ display: 'block', flex: 'none' }}>
    <path d={d} />
  </svg>
);
export const ICONS = {
  min: 'M5 12h14', max: 'M5 5h14v14H5z', restore: 'M7 7h12v12H7zM5 17V5h12', close: 'M6 6l12 12M18 6L6 18',
};

export const Prompt = ({ children, note }: { children: ReactNode; note?: ReactNode }) => (
  <div className="cmd">
    <span className="pr">$</span>
    <span className="txt">{children}</span>
    {note && <span className="muted">{note}</span>}
  </div>
);

export const Fl = ({ children }: { children: ReactNode }) => <span className="fl">{children}</span>;
export const Cursor = () => <span className="cursor" aria-hidden="true" />;
export const Spinner = ({ label }: { label?: string }) => (
  <span className="row tight"><span className="spin">◐</span>{label && <span className="muted">{label}</span>}</span>
);

export const SecCap = ({ cmd = '$', title, note, right }: { cmd?: string; title: string; note?: ReactNode; right?: ReactNode }) => (
  <div className="sec-cap">
    <span className="pr">{cmd}</span>
    <h2>{title}</h2>
    {right}
    {note && <span className="n">{note}</span>}
  </div>
);

export const Box = ({ children, tint, pad, style }: { children: ReactNode; tint?: boolean; pad?: boolean; style?: React.CSSProperties }) => (
  <div className={`box ${tint ? 'tint' : ''} ${pad ? 'pad' : ''}`} style={style}>{children}</div>
);

/* ── 标签 ─────────────────────────────────────────────────── */
export function Pill({ tone = 'dim', children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return <span className={`pill ${tone}`} title={title}>{children}</span>;
}

export function RiskPill({ risk }: { risk: Risk | string }) {
  const tone = risk === 'high' ? 'bad' : risk === 'medium' ? 'req' : 'free';
  return <Pill tone={tone} title={RISK_HINT[risk] || ''}>{RISK_LABELS[risk] || risk}</Pill>;
}

export function KindPill({ kind }: { kind: string }) {
  return <Pill title={`条目类型：${kind}`}>{KIND_LABELS[kind] || kind}</Pill>;
}

const STATUS_TONE: Record<string, string> = {
  succeeded: 'free', ok: 'free', approved: 'free', failed: 'bad', rejected: 'bad', error: 'bad',
  running: 'info', pending: 'info', ready: 'info', awaiting_approval: 'req', waiting: 'req',
  cancelled: 'dim', skipped: 'dim', partial: 'req',
};
const STATUS_LABEL: Record<string, string> = {
  succeeded: 'ok', failed: 'fail', running: 'run…', pending: 'wait', ready: 'ready',
  awaiting_approval: '待批准', waiting: '待复核', cancelled: 'cancel', partial: 'partial',
  skipped: 'skip', approved: 'approved', rejected: 'rejected',
};
export function StatusPill({ status }: { status: string }) {
  return <Pill tone={STATUS_TONE[status] || 'dim'}>{STATUS_LABEL[status] || status}</Pill>;
}

/* ── 日志流 ───────────────────────────────────────────────── */
export const Log = ({ children, style }: { children: ReactNode; style?: React.CSSProperties }) => (
  <div className="log" style={style}>{children}</div>
);

export const LogLine = ({ ts, level = 'info', children }: { ts?: string; level?: 'ok' | 'info' | 'err' | 'warn' | ''; children: ReactNode }) => {
  const tag = level === 'ok' ? '[ OK ]' : level === 'err' ? '[ERR ]' : level === 'warn' ? '[WARN]' : level === 'info' ? '[INFO]' : '';
  return (
    <div className="ln">
      <span className="ts">{ts || ''}</span>
      <span className={`lv ${level}`}>{tag}</span>
      <span style={{ minWidth: 0 }}>{children}</span>
    </div>
  );
};

export const Dots = ({ n = 12 }: { n?: number }) => <span className="dots">{'·'.repeat(Math.max(2, n))}</span>;

/* ── 控件 ─────────────────────────────────────────────────── */
export function Btn({ children, onClick, variant = '', disabled, title, small }: {
  children: ReactNode; onClick?: () => void; variant?: string; disabled?: boolean; title?: string; small?: boolean;
}) {
  return <button type="button" title={title} disabled={disabled} onClick={onClick} className={`btn ${variant} ${small ? 'sm' : ''}`}>{children}</button>;
}

export function Check({ on, onChange, label, hint }: { on: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  return (
    <span className="check" data-on={on ? '1' : '0'} role="switch" aria-checked={on} tabIndex={0} title={hint || label}
      onClick={() => onChange(!on)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onChange(!on); } }}>
      <i>{on ? '[x]' : '[ ]'}</i>
      <span className="t">{label}</span>
      {hint && <span className="h">— {hint}</span>}
    </span>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="field"><label title={hint}>{label}</label>{children}</div>;
}

export function RunLine({ value, onChange, onRun, placeholder, label = '运行 ⏎', disabled, busy }: {
  value: string; onChange: (v: string) => void; onRun: () => void; placeholder?: string; label?: string; disabled?: boolean; busy?: boolean;
}) {
  return (
    <div className="runline">
      <span className="pr">$</span>
      <input value={value} placeholder={placeholder} spellCheck={false} aria-label={label}
        onChange={(e) => onChange(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onRun(); }} />
      <button type="button" onClick={onRun} disabled={disabled || busy}>{busy ? '…' : label}</button>
    </div>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  }
}

export function CopyBlock({ text, label, height }: { text: string; label?: string; height?: number }) {
  const toast = useToast();
  const [shown, setShown] = useState(false);
  return (
    <div style={{ display: 'grid', gap: 5 }}>
      <div className="row tight">
        <span className="muted" style={{ fontSize: 11 }}>{label}</span>
        <span className="right row tight">
          <Btn small variant="ghost" title={shown ? '收起' : '展开完整内容'} onClick={() => setShown(!shown)}>{shown ? '折叠' : '展开'}</Btn>
          <Btn small title="复制到剪贴板" onClick={() => void copyText(text).then((ok) => toast(ok ? '已复制' : '复制失败，请手动选中文本', ok ? 'ok' : 'bad'))}>复制</Btn>
        </span>
      </div>
      <pre className="code" style={{ maxHeight: shown ? 520 : height ?? 170 }}>{text}</pre>
    </div>
  );
}

export const Note = ({ children, tone = '' }: { children: ReactNode; tone?: string }) => (
  <div className={`note ${tone}`}><span className="mark">›</span><div style={{ minWidth: 0 }}>{children}</div></div>
);

export const Empty = ({ title, children }: { title: string; children?: ReactNode }) => (
  <div className="empty"><div className="big">{title}</div>{children}</div>
);

export function Modal({ title, children, onClose, footer }: {
  title: string; children: ReactNode; onClose: () => void; footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ marginBottom: 10 }}>
          <h3 className="grow">{title}</h3>
          <Btn variant="ghost" title="关闭" onClick={onClose}>✕</Btn>
        </div>
        {children}
        {footer && <div className="row right" style={{ marginTop: 14 }}>{footer}</div>}
      </div>
    </div>
  );
}

type Toast = { id: number; text: string; tone: string };
const ToastCtx = createContext<(text: string, tone?: string) => void>(() => {});

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone = '') => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-3), { id, text, tone }]);
    window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), tone === 'bad' ? 7000 : 3200);
  }, []);
  const value = useMemo(() => push, [push]);
  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="pulse">
        {items.map((t) => <div key={t.id} className={`toast ${t.tone}`}>{t.tone === 'bad' ? '✗' : t.tone === 'ok' ? '✓' : '›'} {t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}

/* 字符块条形图 */
export function Bars({ data, width = 26 }: { data: Record<string, number>; width?: number }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]).slice(0, 14);
  const top = Math.max(1, ...entries.map(([, v]) => v));
  return (
    <div className="bars">
      {entries.map(([k, v]) => {
        const filled = Math.max(1, Math.round((v / top) * width));
        return (
          <div className="b" key={k} title={`${k}：${v} 条`}>
            <span className="name">{k}</span>
            <span className="track">{'█'.repeat(filled)}<span style={{ color: 'var(--line)' }}>{'░'.repeat(width - filled)}</span></span>
            <span className="num">{v}</span>
          </div>
        );
      })}
    </div>
  );
}

export const TimeAgo = ({ iso }: { iso: string | null | undefined }) => <span title={iso || ''}>{relativeTime(iso)}</span>;

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const run = useCallback(() => {
    setBusy(true);
    setError(null);
    fn().then((value) => { if (alive.current) setData(value); })
      .catch((exc: Error) => { if (alive.current) setError(exc.message); })
      .finally(() => { if (alive.current) setBusy(false); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(run, [run]);
  return { data, error, busy, reload: run, setData };
}

/* 横向标签（终端风格：$ ls xxx/） */
export function Tabs({ tabs, active, onChange }: { tabs: { id: string; label: string; hint?: string }[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="row tight" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 8 }}>
      {tabs.map((t) => (
        <button key={t.id} title={t.hint} onClick={() => onChange(t.id)}
          className="btn sm" style={active === t.id ? { borderColor: 'var(--green)', color: 'var(--green)', background: 'var(--green-bg)' } : {}}>
          {active === t.id ? '▸ ' : '  '}{t.label}
        </button>
      ))}
    </div>
  );
}

/* ASCII DAG：按依赖分层，节点用方框、连线用字符 */
export type DagNode = { id: string; skill?: string; status?: string; ms?: number | null; caption?: string };
export function Dag({ nodes, edges }: { nodes: DagNode[]; edges: { source: string; target: string }[] }) {
  if (nodes.length === 0) return null;
  const parents: Record<string, string[]> = {};
  nodes.forEach((n) => { parents[n.id] = []; });
  edges.forEach((e) => { if (parents[e.target]) parents[e.target].push(e.source); });
  const depth: Record<string, number> = {};
  const visit = (id: string, seen: Set<string>): number => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const list = parents[id] || [];
    depth[id] = list.length === 0 ? 0 : 1 + Math.max(...list.map((p) => visit(p, seen)));
    return depth[id];
  };
  nodes.forEach((n) => visit(n.id, new Set()));
  const layers: Record<number, DagNode[]> = {};
  nodes.forEach((n) => {
    const d = depth[n.id] ?? 0;
    (layers[d] = layers[d] || []).push(n);
  });
  const rows = Math.max(...Object.values(layers).map((l) => l.length));
  const cols = Object.keys(layers).length;
  return (
    <div className="diagram" role="img" aria-label="工作流节点图">
      {Array.from({ length: rows }).map((_, row) => (
        <div className="drow" key={row}>
          {Array.from({ length: cols }).map((__, col) => {
            const node = (layers[col] || [])[row];
            const link = col < cols - 1 ? '──────▶' : null;
            return (
              <div key={col} style={{ display: 'contents' }}>
                {node ? (
                  <div className={`dnode ${node.status === 'succeeded' ? 'ok' : node.status === 'failed' ? 'bad' : 'dim'}`}>
                    <div className="id">{node.id}</div>
                    <div className="stt">
                      <span className={node.status === 'failed' ? 's-bad' : 's-ok'}>
                        {node.status === 'succeeded' ? '[ ok ]' : node.status === 'failed' ? '[fail]' : node.status ? `[${node.status}]` : '[ — ]'}
                      </span>
                      <span className="ms">{node.ms !== null && node.ms !== undefined ? `${node.ms}ms` : ''}</span>
                    </div>
                  </div>
                ) : <div className="dnode dim" style={{ visibility: 'hidden' }}><div className="id">·</div></div>}
                {link && <span className="dlink">{link}</span>}
              </div>
            );
          })}
        </div>
      ))}
      <div className="dcaps">
        {Array.from({ length: cols }).map((_, col) => {
          const first = (layers[col] || [])[0];
          return (
            <span key={col}>
              {first ? `${first.id} · ${first.skill || ''}${first.caption ? ` · ${first.caption}` : ''}` : ''}
              {col < cols - 1 && <span className="gap" />}
            </span>
          );
        })}
      </div>
    </div>
  );
}
