import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, clockTime, type InvokeResult, type Tool, type ToolDetail } from '../hub';
import { Box, Btn, Check, CopyBlock, Cursor, Dots, Empty, Field, Fl, KindPill, Log, LogLine, Modal, Note, Pill, Prompt, RiskPill, RunLine, SecCap, Spinner, Tabs, TimeAgo, useToast } from '../ui';

const PAGE = 40;
const argvLines = (text: string) => text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

/* ── man 页里的试运行区 ─────────────────────────────────── */
function Trial({ tool, onDone }: { tool: Tool; onDone: () => void }) {
  const [argv, setArgv] = useState('');
  const [stdin, setStdin] = useState('');
  const [timeout, setTimeout_] = useState('');
  const [named, setNamed] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<'' | 'dry' | 'run'>('');
  const [result, setResult] = useState<InvokeResult | null>(null);
  const stopRef = useRef(false);
  useEffect(() => () => { stopRef.current = true; }, []);

  const run = useCallback(async (dry: boolean) => {
    setBusy(dry ? 'dry' : 'run');
    setResult(null);
    stopRef.current = false;
    const inputs: Record<string, unknown> = {};
    const args = argvLines(argv);
    if (args.length) inputs.argv = args;
    if (stdin.trim()) inputs.stdin = stdin;
    if (timeout.trim()) inputs.timeout_seconds = Number(timeout) || undefined;
    const flags = Object.fromEntries(Object.entries(named).filter(([, v]) => v !== ''));
    if (Object.keys(flags).length) inputs.flags = flags;
    if (dry) inputs.dry_run = true;
    try {
      if (dry) {
        setResult(await api.invoke(tool.id, inputs));
      } else {
        const started = await api.invoke(tool.id, inputs, 'workflowhub-ui', false);
        let attempts = 0;
        while (!stopRef.current && attempts < 900) {
          attempts += 1;
          const state = await api.task(String(started.task_id));
          if (state.status !== 'running') {
            setResult(state.result ?? { status: state.status, run_id: null, error: state.error as never });
            break;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 400));
        }
      }
      onDone();
    } catch (exc) {
      setResult({ status: 'failed', run_id: null, error: { code: 'error', message: (exc as Error).message, details: {} } });
    } finally {
      setBusy('');
    }
  }, [argv, named, onDone, stdin, timeout, tool.id]);

  const out = result?.outputs ?? {};
  const stdout = typeof out.stdout === 'string' ? out.stdout : '';
  const stderr = typeof out.stderr === 'string' ? out.stderr : '';
  const command = typeof out.command_line === 'string' ? out.command_line : '';

  return (
    <section>
      <SecCap title="TRY --dry-run 先看清命令" note="执行结果会写进 runs/ 与审计" />
      <div style={{ display: 'grid', gap: 10 }}>
        <div className="grid2" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <Field label="argv（每行一个参数，原样传给命令，不经过 shell）">
            <textarea className="textarea" rows={3} placeholder={'--version\n输出目录'} value={argv} onChange={(e) => setArgv(e.target.value)} />
          </Field>
          <div style={{ display: 'grid', gap: 8 }}>
            <Field label="stdin（可留空）">
              <textarea className="textarea" rows={2} value={stdin} onChange={(e) => setStdin(e.target.value)} />
            </Field>
            <Field label="超时秒数（留空用默认）">
              <input className="input" placeholder="120" value={timeout} onChange={(e) => setTimeout_(e.target.value)} />
            </Field>
          </div>
        </div>
        {tool.args.length > 0 && (
          <Box pad>
            <div className="h1" style={{ fontSize: 12, marginBottom: 6 }}>从源码识别到的参数 <span className="muted">（也可直接用上面的 argv）</span></div>
            <div className="grid3">
              {tool.args.slice(0, 9).map((arg) => (
                <Field key={arg.name} label={`${arg.name} · ${arg.kind}${arg.required ? ' · 必填' : ''}`} hint={arg.description}>
                  <input className="input" placeholder={arg.description || '值'}
                    value={named[arg.name] ?? ''} onChange={(e) => setNamed({ ...named, [arg.name]: e.target.value })} />
                </Field>
              ))}
            </div>
          </Box>
        )}
        <div className="row tight">
          <Btn onClick={() => void run(true)} disabled={busy !== ''}>$ 预览命令{busy === 'dry' && <Spinner />}</Btn>
          <Btn variant="primary" onClick={() => void run(false)} disabled={busy !== ''}>$ 执行{busy === 'run' && <Spinner />}</Btn>
          {result?.status === 'awaiting_approval' && <Pill tone="req">已转成审批请求，请在窗口里批准</Pill>}
        </div>
        {result && (
          <Log>
            <LogLine level={result.status === 'succeeded' ? 'ok' : result.status === 'awaiting_approval' ? 'warn' : 'err'}>
              {result.status === 'succeeded' ? 'run finished' : result.status} <Dots n={10} />{' '}
              {out.exit_code !== undefined && out.exit_code !== null && <>exit={String(out.exit_code)} </>}
              {out.duration_ms !== undefined && <span className="am">{String(out.duration_ms)}ms </span>}
              {result.run_id && <span className="path">{result.run_id.slice(0, 12)}</span>}
              {out.truncated === true && <span className="am"> · 输出被截断</span>}
            </LogLine>
            {command && <LogLine ts="cmd" level="info"><span className="muted">{command}</span></LogLine>}
            {stdout && <LogLine ts="stdout" level="ok"><span style={{ whiteSpace: 'pre-wrap' }}>{stdout.slice(0, 20000)}</span></LogLine>}
            {stderr && <LogLine ts="stderr" level="err"><span style={{ whiteSpace: 'pre-wrap' }}>{stderr.slice(0, 12000)}</span></LogLine>}
            {result.error && <LogLine ts="error" level="err"><span className="er">{result.error.message}</span>
              {Object.keys(result.error.details || {}).length > 0 && <span className="muted"> {JSON.stringify(result.error.details).slice(0, 300)}</span>}</LogLine>}
            {(result.artifacts ?? []).map((a) => (
              <LogLine key={a.uri} ts="file" level="info">
                <a className="path" href={`/api/v1/files?path=${encodeURIComponent(a.uri)}&download=true`} title="下载这个输出文件">
                  {a.uri.split(/[\\/]/).pop()} ({a.size_bytes ?? 0}B)
                </a>
              </LogLine>
            ))}
            <LogLine><Cursor /></LogLine>
          </Log>
        )}
      </div>
    </section>
  );
}

/* ── man 页 ─────────────────────────────────────────────── */
function ManPage({ toolKey, onClose, onChanged }: { toolKey: string; onClose: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [detail, setDetail] = useState<ToolDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState('curl');
  const [notes, setNotes] = useState('');
  const [alias, setAlias] = useState('');
  const [editing, setEditing] = useState(false);

  const load = useCallback(() => {
    api.tool(toolKey).then((data) => {
      setDetail(data); setNotes(data.tool.notes); setAlias(data.tool.alias); setError(null);
    }).catch((exc: Error) => setError(exc.message));
  }, [toolKey]);
  useEffect(load, [load]);

  const patch = useCallback(async (body: { enabled?: boolean; starred?: boolean }) => {
    await api.setState(toolKey, body);
    load(); onChanged();
  }, [toolKey, load, onChanged]);

  return (
    <div className="man-mask" onClick={onClose}>
      <div className="man" onClick={(e) => e.stopPropagation()}>
        {error && <Note tone="bad">{error}</Note>}
        {!detail && !error && <Spinner label="读取 man 页…" />}
        {detail && (
          <>
            <div className="head">
              <span className="pr" style={{ color: 'var(--green)', fontWeight: 600 }}>man</span>
              <span className="name">{detail.tool.title}</span>
              <KindPill kind={detail.tool.kind} />
              <RiskPill risk={detail.tool.risk} />
              {detail.tool.enabled ? <Pill tone="free">已启用</Pill> : <Pill tone="req">未启用</Pill>}
              {detail.tool.blueprint && <Pill tone="info">蓝图</Pill>}
              <Btn small variant="ghost" onClick={onClose}>✕ esc</Btn>
            </div>

            <div className="row tight">
              <Check on={detail.tool.enabled} label="允许 Agent 调用" hint="关闭后调用会返回 permission_error"
                onChange={(v) => void patch({ enabled: v })} />
              <Btn small onClick={() => void patch({ starred: !detail.tool.starred })} title="星标排在前面">
                {detail.tool.starred ? '★ 已收藏' : '☆ 收藏'}
              </Btn>
              <Btn small onClick={() => void api.probe(detail.tool.id).then((r) => {
                toast(r.ok ? `版本：${r.output.slice(0, 80)}` : '这个命令不响应 --version（正常）', r.ok ? 'ok' : '');
                load();
              })}>--version 探测</Btn>
              {detail.tool.kind === 'custom' && (
                <Btn small variant="danger" onClick={() => void api.del(`/api/v1/tools/${detail.tool.id}`).then(() => { toast('已删除'); onChanged(); onClose(); })}>删除</Btn>
              )}
            </div>

            <section>
              <SecCap title="NAME / DESCRIPTION" />
              <Box pad>
                <div style={{ whiteSpace: 'pre-wrap' }}>{detail.tool.description || '（没有说明）'}</div>
                <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
                  <div>key     <Fl>{detail.tool.id}</Fl></div>
                  <div>command <span className="soft">{detail.tool.command.join(' ') || '（无入口）'}</span></div>
                  <div>source  <span className="soft">{detail.tool.source}</span></div>
                  {detail.tool.loc && <div>path    <span className="soft">{detail.tool.loc}</span></div>}
                  {detail.tool.cwd && <div>cwd     <span className="soft">{detail.tool.cwd}</span></div>}
                  <div>calls   <span className="soft">{detail.tool.invocations} 次 · 最近 <TimeAgo iso={detail.tool.last_invoked} /> · 发现于 <TimeAgo iso={detail.tool.found_at} /></span></div>
                  {detail.tool.probe?.output && <div>version <span className="soft">{detail.tool.probe.output.slice(0, 160)}</span></div>}
                </div>
              </Box>
              {detail.tool.blueprint && (
                <div style={{ marginTop: 8 }}>
                  <Note tone="info">{detail.tool.call_hint}
                    {detail.tool.kind === 'skill' && (
                      <span style={{ display: 'block', marginTop: 8 }}>
                        <Btn small variant="primary" onClick={() => void api.adopt(detail.tool.id).then((r) => {
                          toast(`已接入 skills/${r.id}，可编排进工作流`, 'ok'); onChanged(); load();
                        }).catch((exc: Error) => toast(exc.message, 'bad'))}>$ 接入为本地 skill</Btn>
                        <span className="muted"> 复制 skill.yaml + handler 到 skills/，交给 python 执行器</span>
                      </span>
                    )}
                  </Note>
                </div>
              )}
              <div style={{ marginTop: 8 }}>
                <Btn small variant="ghost" onClick={() => setEditing(!editing)}>{editing ? '收起' : '加备注 / 改别名'}</Btn>
                {editing && (
                  <Box pad style={{ marginTop: 6 }}>
                    <div className="grid2">
                      <Field label="别名（小写字母/数字/连字符，Agent 会看到它）">
                        <input className="input" value={alias} placeholder={detail.tool.id} onChange={(e) => setAlias(e.target.value)} />
                      </Field>
                      <Field label="备注：它在你工作里的用途">
                        <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
                      </Field>
                    </div>
                    <div style={{ marginTop: 8 }}>
                      <Btn small variant="primary" onClick={() => void api.setNote(detail.tool.id, { notes, alias }).then(() => {
                        toast('已保存', 'ok'); onChanged(); load(); setEditing(false);
                      })}>保存</Btn>
                    </div>
                  </Box>
                )}
              </div>
            </section>

            {detail.skill && (
              <section>
                <SecCap title="PARAMETERS" note="Agent 用这些字段调用" />
                <Box>
                  <table className="ls">
                    <thead><tr><th>字段</th><th style={{ width: 74 }}>类型</th><th style={{ width: 58 }}>必填</th><th>说明</th></tr></thead>
                    <tbody>
                      {Object.entries(detail.skill.inputs).map(([name, spec]) => (
                        <tr key={name} style={{ cursor: 'default' }}>
                          <td className="nowrap">{name}</td>
                          <td><Pill>{spec.type}</Pill></td>
                          <td>{spec.required ? <Pill tone="bad">必填</Pill> : <span className="muted">可选</span>}</td>
                          <td className="desc">{spec.description}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Box>
              </section>
            )}

            {!detail.tool.blueprint && <Trial tool={detail.tool} onDone={() => { onChanged(); load(); }} />}

            <section>
              <SecCap title="CALL EXAMPLES" note="复制给 Agent 即用" />
              <Tabs active={tab} onChange={setTab} tabs={[
                { id: 'curl', label: 'REST', hint: '最直接的 HTTP 调用' },
                { id: 'python', label: 'Python', hint: 'requests 片段' },
                { id: 'function_calling', label: 'Function calling', hint: 'OpenAI/DeepSeek tools 结构' },
                { id: 'mcp', label: 'MCP', hint: '一次配置全部工具可用' },
              ]} />
              <CopyBlock text={detail.snippets[tab] || '（无）'} height={150} />
              <div style={{ marginTop: 6 }}><Note>把 key 换成 <Fl>{detail.tool.id}</Fl>，其它工具同理；<b>dry_run:true</b> 只回显命令行不执行。</Note></div>
            </section>

            <section>
              <SecCap title="AUDIT --this" note="这个工具的最近调用" />
              {detail.audit.length === 0 ? <Box pad><span className="muted">还没有调用记录。</span></Box> : (
                <Log>
                  {detail.audit.slice(0, 8).map((a, i) => (
                    <LogLine key={`${a.at}${i}`} ts={clockTime(a.at)} level={a.status === 'succeeded' ? 'ok' : 'err'}>
                      <span className="muted">{a.actor || 'agent'}</span> <Dots n={Math.max(3, 16 - (a.actor || '').length)} /> {a.status}
                      {a.exit_code !== undefined && a.exit_code !== null && <> exit={a.exit_code}</>}
                      {a.command_line && <span className="muted"> · {String(a.command_line).slice(0, 140)}</span>}
                    </LogLine>
                  ))}
                </Log>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}

/* ── 手动登记 ───────────────────────────────────────────── */
function CustomModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [cwd, setCwd] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <Modal title="登记一个自定义命令" onClose={onClose}
      footer={<>
        <Btn variant="ghost" onClick={onClose}>取消</Btn>
        <Btn variant="primary" disabled={busy} onClick={() => {
          setBusy(true);
          api.addCustom({ name, command: argvLines(command), cwd: cwd || null, description })
            .then((row) => { toast(`已登记：${row.title}`, 'ok'); onSaved(); onClose(); })
            .catch((exc: Error) => toast(exc.message, 'bad'))
            .finally(() => setBusy(false));
        }}>登记</Btn>
      </>}>
      <div style={{ display: 'grid', gap: 10 }}>
        <Note>扫描没覆盖到的命令可以在这里补：命令拆成多行，第一行是可执行文件的<b>绝对路径</b>，后面每行是一个固定前缀参数。</Note>
        <Field label="名称"><input className="input" value={name} placeholder="我的构建脚本" onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="命令（每行一片）">
          <textarea className="textarea" rows={3} value={command} placeholder={'C:\\Program Files\\Git\\bin\\git.exe\nstatus'} onChange={(e) => setCommand(e.target.value)} />
        </Field>
        <div className="grid2">
          <Field label="工作目录（可留空）"><input className="input" value={cwd} onChange={(e) => setCwd(e.target.value)} /></Field>
          <Field label="说明（Agent 会看到）"><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        </div>
      </div>
    </Modal>
  );
}

/* ── 工具库主页面 ───────────────────────────────────────── */
export default function ToolsView({ focus, onFocusDone, onChanged }: {
  focus: string | null; onFocusDone: () => void; onChanged: () => void;
}) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [category, setCategory] = useState('all');
  const [risk, setRisk] = useState('all');
  const [scope, setScope] = useState<'all' | 'enabled' | 'starred'>('all');
  const [rows, setRows] = useState<Tool[]>([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(focus);
  const [custom, setCustom] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (focus) { setOpen(focus); onFocusDone(); } }, [focus, onFocusDone]);

  const params = useMemo(() => ({
    query, kind, category, risk, limit: PAGE,
    enabled: scope === 'enabled' ? true : undefined,
    starred: scope === 'starred' ? true : undefined,
  }), [query, kind, category, risk, scope]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setBusy(true);
      api.tools(params).then((list) => { setRows(list.items); setTotal(list.total); setFacets(list.facets.categories); })
        .finally(() => setBusy(false));
    }, query ? 240 : 0);
    return () => window.clearTimeout(timer);
  }, [params, query]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA') { e.preventDefault(); searchRef.current?.focus(); }
      if (e.key === 'Escape' && open) setOpen(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  const loadMore = () => {
    setBusy(true);
    api.tools({ ...params, offset: rows.length })
      .then((list) => setRows((prev) => [...prev, ...list.items]))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Prompt note={<span className="muted">这台电脑上能被 Agent 调用的东西都在这；点开看 man 页</span>}>
        workflowhub ls tools/ <Fl>--search</Fl>
      </Prompt>

      <Box pad>
        <RunLine value={query} onChange={setQuery} onRun={() => searchRef.current?.blur()}
          placeholder="ffmpeg、下载、pdf、npm、C:\脚本…" label="搜索" />
        <div className="row tight" style={{ marginTop: 10 }}>
          <select className="select" value={kind} title="按类型筛选" onChange={(e) => setKind(e.target.value)}>
            <option value="all">类型 全部</option>
            <option value="cli">可执行程序</option><option value="script">脚本工具</option>
            <option value="app">已安装应用</option><option value="project-script">项目入口</option>
            <option value="skill">Skill 包</option><option value="custom">手动登记</option>
          </select>
          <select className="select" value={category} title="按分类筛选" onChange={(e) => setCategory(e.target.value)}>
            <option value="all">分类 全部</option>
            {Object.entries(facets).sort((a, b) => b[1] - a[1]).map(([k, v]) => <option key={k} value={k}>{k}（{v}）</option>)}
          </select>
          <select className="select" value={risk} title="按风险筛选" onChange={(e) => setRisk(e.target.value)}>
            <option value="all">风险 全部</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option>
          </select>
          {(['all', 'enabled', 'starred'] as const).map((v) => (
            <Btn key={v} small variant={scope === v ? 'primary' : ''} onClick={() => setScope(v)}
              title={v === 'enabled' ? '只看 Agent 当前能调的' : v === 'starred' ? '只看收藏' : '全部条目'}>
              {v === 'all' ? '全部' : v === 'enabled' ? '可用' : '收藏'}
            </Btn>
          ))}
          <span className="right row tight">
            <span className="muted">{busy ? '读取中…' : `命中 ${total} · 已显示 ${rows.length}`}</span>
            <Btn small onClick={() => setCustom(true)}>+ 登记命令</Btn>
          </span>
        </div>
      </Box>

      {rows.length === 0 && !busy ? (
        <Empty title="没有匹配的条目">
          {total === 0 && !query ? '目录是空的：Ctrl+R 扫描本机，或在设置里添加扫描目录。' : '换个关键词，或把类型/分类/风险筛选放开。'}
        </Empty>
      ) : (
        <Box>
          <table className="ls">
            <thead>
              <tr><th style={{ width: 34 }}>#</th><th>名称</th><th style={{ width: 96 }}>类型</th><th style={{ width: 74 }}>风险</th>
                <th style={{ width: 108 }}>分类</th><th style={{ width: 56 }}>调用</th><th>说明</th></tr>
            </thead>
            <tbody>
              {rows.map((tool, i) => (
                <tr key={tool.id} className={open === tool.id ? 'sel' : ''} onClick={() => setOpen(tool.id)}>
                  <td className="muted nowrap">{String(i + 1).padStart(2, '0')}</td>
                  <td className="nowrap">
                    {tool.starred && <span style={{ color: 'var(--amber)' }}>★ </span>}
                    <Fl>{tool.title}</Fl>
                    {!tool.enabled && <span className="pill req"> 停用</span>}
                    {tool.blueprint && <span className="pill info"> 蓝图</span>}
                  </td>
                  <td><KindPill kind={tool.kind} /></td>
                  <td><RiskPill risk={tool.risk} /></td>
                  <td className="desc nowrap">{tool.category}</td>
                  <td className="nowrap">{tool.invocations ? `×${tool.invocations}` : <span className="muted">—</span>}</td>
                  <td className="desc">{tool.description.slice(0, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Box>
      )}
      {rows.length < total && <Btn onClick={loadMore} disabled={busy}>↓ 加载更多（还剩 {total - rows.length}）</Btn>}
      <Note tone="info">点开任意一行看 man 页：它是什么、参数怎么传、试运行、给 Agent 的调用片段、最近审计都在里面。命令以 exec 启动不经 shell。</Note>
      {open && <ManPage toolKey={open} onClose={() => setOpen(null)} onChanged={onChanged} />}
      {custom && <CustomModal onClose={() => setCustom(false)} onSaved={() => { onChanged(); setQuery(''); }} />}
    </>
  );
}
