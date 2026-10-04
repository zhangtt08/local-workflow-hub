import { useEffect, useState } from 'react';
import { api, relativeTime, type Overview, type SystemInfo } from '../hub';
import { Box, Btn, Check, CopyBlock, Field, Note, Pill, Prompt, SecCap, Spinner, useToast } from '../ui';

export default function SettingsView({ overview, onReload, onRescan }: {
  overview: Overview | null; onReload: () => void; onRescan: () => void;
}) {
  const toast = useToast();
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [roots, setRoots] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api.system().then(setSystem).catch(() => undefined);
    void api.scanStatus().then((data) => { setRoots(data.roots); setErrors(data.errors); }).catch(() => undefined);
    void api.settings().then((data) => setSettings(data.settings)).catch(() => undefined);
  }, []);

  const save = async (patch: Record<string, unknown>, message: string) => {
    setBusy(true);
    try {
      const data = await api.saveSettings(patch);
      setSettings(data.settings);
      toast(message, 'ok');
      onReload();
    } catch (exc) {
      toast((exc as Error).message, 'bad');
    } finally {
      setBusy(false);
    }
  };

  const num = (key: string, fallback: number) => Number(settings[key] ?? fallback);

  return (
    <>
      <Prompt note={<span className="muted">这里决定 WorkflowHub 看得见什么、Agent 能碰到什么；改完立即生效</span>}>
        workflowhub config --edit
      </Prompt>

      <div className="grid2">
        <section>
          <SecCap title="CONFIG scan_roots" note="脚本与项目入口的来源" />
          <Box>
            <table className="ls">
              <tbody>
                {roots.map((root) => (
                  <tr key={root} style={{ cursor: 'default' }}>
                    <td><span className="fl">{root}</span><div className="muted" style={{ fontSize: 11 }}>深度 4 层 · 跳过 node_modules/.git/venv</div></td>
                    <td style={{ width: 60 }}>
                      <Btn small variant="ghost" title="移出扫描范围"
                        onClick={() => void save({ scan_roots: roots.filter((r) => r !== root) }, '已移除该目录')}>✕</Btn>
                    </td>
                  </tr>
                ))}
                {roots.length === 0 && <tr><td className="muted">还没有扫描目录</td></tr>}
              </tbody>
            </table>
          </Box>
          <div className="row tight" style={{ marginTop: 8 }}>
            <span className="grow"><input className="input" placeholder="C:\\Users\\你\\Projects 或 D:\\脚本" value={draft}
              onChange={(e) => setDraft(e.target.value)} /></span>
            <Btn onClick={() => {
              if (!draft.trim()) { toast('先填一个目录路径'); return; }
              void save({ scan_roots: [...roots, draft.trim()] }, '已添加，记得重新扫描');
              setRoots([...roots, draft.trim()]);
              setDraft('');
            }}>添加</Btn>
            <Btn variant="primary" onClick={onRescan}>↻ 立即重扫</Btn>
          </div>
          {errors.length > 0 && <div style={{ marginTop: 8 }}><Note tone="warn">{errors.map((e) => <div key={e}>{e}</div>)}</Note></div>}
        </section>

        <section>
          <SecCap title="CONFIG risk_policy" note="决定 Agent 能碰多远" />
          <Box pad>
            <div style={{ display: 'grid', gap: 10 }}>
              <Check on={Boolean(settings.high_risk_enabled)} label="允许调用高风险工具（python / powershell / cmd / curl…）"
                hint="关闭后这些工具对 Agent 一律返回 permission_error"
                onChange={(v) => void save({ high_risk_enabled: v }, v ? '已允许高风险工具' : '已关闭高风险工具')} />
              <Check on={Boolean(settings.approval_mode)} label="审批模式：高风险调用先由你在窗口里批准"
                hint="开启后 Agent 收到 awaiting_approval，需在标题栏「待批」放行"
                onChange={(v) => void save({ approval_mode: v }, v ? '审批模式已开启' : '审批模式已关闭')} />
              <div className="grid2">
                <Field label="默认超时（秒）">
                  <input className="input" type="number" min={1} max={3600} value={num('default_timeout_seconds', 120)}
                    onChange={(e) => setSettings({ ...settings, default_timeout_seconds: e.target.value })}
                    onBlur={(e) => void save({ default_timeout_seconds: Number(e.target.value) || 120 }, '超时已保存')} />
                </Field>
                <Field label="输出上限（字节）">
                  <input className="input" type="number" min={2000} step={10000} value={num('max_output_bytes', 200000)}
                    onChange={(e) => setSettings({ ...settings, max_output_bytes: e.target.value })}
                    onBlur={(e) => void save({ max_output_bytes: Number(e.target.value) || 200000 }, '输出上限已保存')} />
                </Field>
              </div>
              <Note>命令以 <b>exec</b> 语义启动、不经过 shell；参数含 NUL 会被拒绝；输出超限会截断并标记 truncated。这是防失控日志，不是安全沙箱。</Note>
            </div>
          </Box>
        </section>
      </div>

      <div className="grid2">
        <section>
          <SecCap title="SYSINFO" note="路径与运行时" />
          {!system ? <Spinner label="读取系统信息…" /> : (
            <Box pad>
              <div className="muted" style={{ fontSize: 11.5, display: 'grid', gap: 3 }}>
                <div>形态      {system.app.frozen ? '桌面 exe' : 'Python 源码'} · 窗口 {system.app.desktop_mode ? '已接管' : '浏览器预览'}</div>
                <div>执行器    {system.executors.join(' · ')}（command=本机命令，python=skill handler）</div>
                <div>python    {system.machine.python} · 主机 {system.machine.node}</div>
                <div>鉴权      {system.token_required ? '需要 WORKFLOWHUB_TOKEN' : '本机直连（仅 127.0.0.1）'}</div>
                <div>数据目录  {system.paths.data}</div>
                <div>界面      {system.paths.ui || '未构建'}</div>
              </div>
              <div className="row tight" style={{ marginTop: 10 }}>
                <Btn small onClick={() => void api.invoke('sys-explorer', { argv: [system.paths.data] }, 'workflowhub-ui')
                  .then(() => toast('已在资源管理器打开数据目录', 'ok'))
                  .catch((exc: Error) => toast(`打不开：${exc.message}`, 'bad'))}
                  title="调用本机 explorer 命令打开数据目录">打开数据目录</Btn>
                <Btn small variant="ghost" onClick={() => void navigator.clipboard.writeText('python -m workflowhub doctor')
                  .then(() => toast('自检命令已复制', 'ok')).catch(() => undefined)}>复制自检命令</Btn>
              </div>
            </Box>
          )}
        </section>

        <section>
          <SecCap title="STATUS scan" note="上次扫描与规模" />
          <Box pad>
            <div className="row tight">
              <Pill>条目 {overview?.catalog.total ?? '—'}</Pill>
              <Pill tone="free">可用 {overview?.catalog.enabled ?? '—'}</Pill>
              <Pill tone="info">skill {overview?.skills.count ?? '—'}</Pill>
              <Pill tone="dim">workflow {overview?.workflows.count ?? '—'}</Pill>
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
              上次扫描：{overview?.last_scan?.at ? relativeTime(String(overview.last_scan.at)) : '从未'}
              {overview?.scan_state?.running ? ` · 正在${overview.scan_state.phase}（${overview.scan_state.found}）` : ''}
            </div>
            <CopyBlock label="cat data/catalog/catalog.json（节选）" height={140}
              text={JSON.stringify({ settings, last_scan: overview?.last_scan ?? {} }, null, 2)} />
            <div style={{ marginTop: 8 }}>
              <Note>工具状态（启用/星标/备注别名）与扫描目录都持久化在这个文件里；换机器时把 <b>data/</b> 一起拷走即可。
                运行数据在 data/runs、data/artifacts、data/catalog/audit.jsonl。</Note>
            </div>
          </Box>
        </section>
      </div>
      {busy && <div style={{ marginTop: 8 }}><Spinner label="保存中…" /></div>}
    </>
  );
}
