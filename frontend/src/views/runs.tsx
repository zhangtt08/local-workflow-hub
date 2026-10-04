import { useCallback, useEffect, useState } from 'react';
import { api, clockTime, relativeTime, type Run, type RunSummary } from '../hub';
import { Box, Btn, CopyBlock, Dag, Empty, Fl, Log, LogLine, Note, Pill, Prompt, SecCap, Spinner, StatusPill, Tabs, useToast } from '../ui';

function RunDetail({ run, onRerun }: { run: Run; onRerun: () => void }) {
  const [file, setFile] = useState<{ path: string; text: string } | null>(null);
  const nodes = Object.values(run.node_runs);
  const edges = (((run as unknown as { definition_snapshot?: { edges?: { source: string; target: string }[] } }).definition_snapshot)?.edges) || [];
  const single = run.workflow_id.startsWith('call-');
  return (
    <div style={{ display: 'grid', gap: 14, minWidth: 0 }}>
      <Box pad>
        <div className="row tight">
          <b>{single ? run.workflow_id.replace(/^call-/, '') : run.workflow_id}</b>
          <Pill>v{run.workflow_version_id.split(':').pop()}</Pill>
          <StatusPill status={run.status} />
          <Pill tone={single ? 'info' : 'dim'}>{single ? '单工具调用' : 'DAG 工作流'}</Pill>
          <span className="right muted">
            {run.started_at ? relativeTime(run.started_at) : '—'} → {run.finished_at ? relativeTime(run.finished_at) : '进行中'}
          </span>
        </div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>
          run {run.id} · trace {run.trace_id}
        </div>
        {run.error && <div style={{ marginTop: 8 }}><Note tone="bad">{run.error.message}</Note></div>}
        {single && <div className="row" style={{ marginTop: 8 }}><Btn small onClick={onRerun}>↻ 用同样输入重跑</Btn></div>}
      </Box>

      {run.error?.details && Object.keys(run.error.details).length > 0 && (
        <pre className="code">{JSON.stringify(run.error.details, null, 2).slice(0, 1500)}</pre>
      )}

      {nodes.length > 0 && <Dag nodes={nodes.map((n) => ({ id: n.node_id, skill: n.skill_id, status: n.status }))} edges={edges} />}

      <section>
        <SecCap title="NODES" note={`${nodes.length} 个`} />
        <Box>
          <table className="ls">
            <thead><tr><th style={{ width: 110 }}>节点</th><th>Skill</th><th style={{ width: 96 }}>状态</th><th style={{ width: 56 }}>尝试</th><th>错误</th></tr></thead>
            <tbody>
              {nodes.map((node) => (
                <tr key={node.id} style={{ cursor: 'default' }}>
                  <td className="nowrap"><Fl>{node.node_id}</Fl></td>
                  <td className="nowrap desc">{node.skill_id}@{node.skill_version}</td>
                  <td><StatusPill status={node.status} /></td>
                  <td>{node.attempt}</td>
                  <td className="desc" style={{ color: 'var(--red)' }}>{node.result?.error?.message || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Box>
      </section>

      {nodes.map((node) => (
        <section key={node.id}>
          <SecCap title={`CAT nodes/${node.node_id}.out`} note={node.skill_id} />
          <Log>
            <LogLine ts="in" level="info"><span className="muted">{JSON.stringify(node.inputs).slice(0, 400) || '{}'}</span></LogLine>
            {node.result?.logs?.slice(0, 40).map((line, i) => (
              <LogLine key={i} ts={String(i + 1).padStart(2, '0')} level={i === 0 ? 'ok' : ''}><span style={{ whiteSpace: 'pre-wrap' }}>{line.slice(0, 600)}</span></LogLine>
            ))}
            {node.result?.outputs && Object.keys(node.result.outputs).length > 0 && (
              <LogLine ts="out" level="ok"><span style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(node.result.outputs).slice(0, 2000)}</span></LogLine>
            )}
            {(node.result?.artifacts ?? []).map((a) => (
              <LogLine key={a.uri} ts="file" level="info">
                <button className="path" style={{ textDecoration: 'underline' }} onClick={() => void api.file(a.uri).then((d) => setFile({ path: a.uri, text: d.text }))}>
                  {a.uri.split(/[\\/]/).pop()} ({a.size_bytes ?? 0}B)
                </button>
              </LogLine>
            ))}
          </Log>
        </section>
      ))}

      {file && (
        <section>
          <SecCap title="ARTIFACT" note={file.path} />
          <pre className="code" style={{ maxHeight: 300 }}>{file.text.slice(0, 20000)}</pre>
        </section>
      )}

      <CopyBlock label="同样的调用给 Agent 用（POST /api/v1/tools/&lt;key&gt;/invoke）"
        text={JSON.stringify({ inputs: run.inputs }, null, 2)} height={120} />
    </div>
  );
}

export default function RunsView({ onChanged }: { onChanged: () => void }) {
  const toast = useToast();
  const [tab, setTab] = useState('all');
  const [rows, setRows] = useState<RunSummary[]>([]);
  const [open, setOpen] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    void api.runs(60, tab === 'all' ? undefined : tab).then((data) => setRows(data.items)).finally(() => setBusy(false));
  }, [tab]);
  useEffect(load, [load]);

  const rerun = async (run: Run) => {
    const key = rows.find((r) => r.id === run.id)?.catalog_key || run.workflow_id.replace(/^call-/, '');
    const result = await api.invoke(key, run.inputs as Record<string, unknown>);
    toast(`重跑结束：${result.status}`, result.status === 'succeeded' ? 'ok' : 'bad');
    onChanged();
    load();
  };

  const counts = rows.reduce<Record<string, number>>((acc, row) => { acc[row.status] = (acc[row.status] || 0) + 1; return acc; }, {});

  return (
    <>
      <Prompt note={<span className="muted">每次调用与每次执行都留完整痕迹；审计在 data/catalog/audit.jsonl</span>}>
        workflowhub tail runs/
      </Prompt>

      <Tabs active={tab} onChange={setTab} tabs={[
        { id: 'all', label: `全部 ${rows.length}`, hint: '不过滤状态' },
        { id: 'succeeded', label: `成功 ${counts.succeeded || ''}` },
        { id: 'failed', label: `失败 ${counts.failed || ''}`, hint: '排查从这里开始' },
        { id: 'running', label: '进行中' },
      ]} />

      <div className="grid2" style={{ gridTemplateColumns: open ? 'minmax(300px, 380px) 1fr' : '1fr', alignItems: 'start' }}>
        <section>
          {busy && rows.length === 0 && <Spinner label="读取记录…" />}
          {!busy && rows.length === 0 && (
            <Empty title="tail: runs/ 为空">在工具库里试运行一次，或让 Agent 调用一次，这里就会留下可回看的痕迹。</Empty>
          )}
          {rows.length > 0 && (
            <Box>
              <table className="ls">
                <thead><tr><th style={{ width: 84 }}>RUN</th><th>目标</th><th style={{ width: 84 }}>状态</th><th style={{ width: 74 }}>时间</th></tr></thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id} className={open?.id === row.id ? 'sel' : ''} onClick={() => void api.run(row.id).then(setOpen)}>
                      <td className="nowrap"><Fl>{row.id.slice(0, 7)}</Fl></td>
                      <td>
                        {row.kind === 'single-tool' ? (row.catalog_key || row.workflow_id) : row.workflow_id}
                        <div className="muted" style={{ fontSize: 11 }}>{row.kind === 'single-tool' ? '单工具' : `${row.nodes.length} 节点`}{row.error ? ` · ${row.error.slice(0, 40)}` : ''}</div>
                      </td>
                      <td className="nowrap"><StatusPill status={row.status} /></td>
                      <td className="nowrap desc">{clockTime(row.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Box>
          )}
        </section>
        {open && <RunDetail run={open} onRerun={() => void rerun(open).catch((exc: Error) => toast(exc.message, 'bad'))} />}
      </div>
    </>
  );
}
