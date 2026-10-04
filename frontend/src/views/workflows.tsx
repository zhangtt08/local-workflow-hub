import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, type Run, type WorkflowRow } from '../hub';
import { Box, Btn, Dag, Empty, Fl, Note, Pill, Prompt, SecCap, Spinner, StatusPill, useToast, type DagNode } from '../ui';

const TEMPLATE = JSON.stringify({
  id: 'my-flow', name: '示例：读取文本、转换、写文件', version: 1,
  inputs: { text: { type: 'string', required: true, description: '要处理的文本' } },
  outputs: { file_path: { type: 'node_output', node_id: 'write', field: 'file_path' } },
  nodes: [
    { id: 'read', type: 'skill', skill_id: 'echo', skill_version: '1.0.0',
      inputs: { text: { type: 'workflow_input', name: 'text' } } },
    { id: 'transform', type: 'skill', skill_id: 'text-transform', skill_version: '1.0.0',
      inputs: { text: { type: 'node_output', node_id: 'read', field: 'text' },
        operation: { type: 'literal', value: 'upper' } } },
    { id: 'write', type: 'skill', skill_id: 'file-writer', skill_version: '1.0.0',
      inputs: { text: { type: 'node_output', node_id: 'transform', field: 'text' },
        filename: { type: 'literal', value: 'out.txt' } } },
  ],
  edges: [{ source: 'read', target: 'transform' }, { source: 'transform', target: 'write' }],
}, null, 2);

type Node = { id: string; skill_id?: string; type?: string };

function edgesOf(doc: Record<string, unknown> | null) {
  const raw = Array.isArray(doc?.edges) ? (doc?.edges as Record<string, unknown>[]) : [];
  return raw.filter((e) => typeof e.source === 'string' && typeof e.target === 'string')
    .map((e) => ({ source: String(e.source), target: String(e.target) }));
}
function nodesOf(doc: Record<string, unknown> | null): Node[] {
  return Array.isArray(doc?.nodes) ? (doc?.nodes as Node[]) : [];
}

function runNodes(run: Run): DagNode[] {
  return Object.values(run.node_runs).map((node) => ({
    id: node.node_id, skill: node.skill_id, status: node.status,
    ms: node.started_at && node.finished_at ? new Date(node.finished_at).getTime() - new Date(node.started_at).getTime() : null,
  }));
}

export default function WorkflowsView({ onChanged }: { onChanged: () => void }) {
  const toast = useToast();
  const [rows, setRows] = useState<WorkflowRow[]>([]);
  const [selected, setSelected] = useState<WorkflowRow | null>(null);
  const [text, setText] = useState(TEMPLATE);
  const [inputs, setInputs] = useState('{\n  "text": "hello workflowhub"\n}');
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<{ valid: boolean; errors: string[]; warnings: string[] } | null>(null);
  const [run, setRun] = useState<Run | null>(null);

  const load = useCallback(() => {
    void api.workflows().then((data) => {
      setRows(data.items);
      setSelected((prev) => data.items.find((i) => i.id === prev?.id && i.version === prev?.version) ?? data.items[0] ?? null);
    });
  }, []);
  useEffect(load, [load]);

  useEffect(() => {
    if (!selected) return;
    setBusy(true);
    void api.workflow(selected.id, selected.version).then((doc) => setText(JSON.stringify(doc, null, 2)))
      .finally(() => setBusy(false));
  }, [selected]);

  const parsed = useMemo<{ doc: Record<string, unknown> | null; error: string | null }>(() => {
    try {
      return { doc: JSON.parse(text) as Record<string, unknown>, error: null };
    } catch (exc) {
      return { doc: null, error: (exc as Error).message };
    }
  }, [text]);

  const validate = () => {
    if (!parsed.doc) { toast('JSON 还没解析通过：' + parsed.error, 'bad'); return; }
    setBusy(true);
    void api.validateWorkflow(parsed.doc).then((data) => {
      setReport(data);
      toast(data.valid ? '校验通过，可以发布' : `${data.errors.length} 个错误要修`, data.valid ? 'ok' : 'bad');
    }).catch((exc: Error) => toast(exc.message, 'bad')).finally(() => setBusy(false));
  };

  const save = () => {
    if (!parsed.doc) return;
    setBusy(true);
    void api.saveWorkflow(parsed.doc).then((data) => {
      toast(`已发布 ${data.workflow_version_id}`, 'ok');
      setReport({ valid: true, errors: [], warnings: data.warnings });
      load(); onChanged();
    }).catch((exc: Error) => toast(exc.message, 'bad')).finally(() => setBusy(false));
  };

  const execute = () => {
    if (!selected) return;
    let parsedInputs: Record<string, unknown>;
    try {
      parsedInputs = JSON.parse(inputs) as Record<string, unknown>;
    } catch (exc) {
      toast('运行输入不是合法 JSON：' + (exc as Error).message, 'bad');
      return;
    }
    setBusy(true); setRun(null);
    void api.runWorkflow(selected.id, selected.version, parsedInputs).then((data) => {
      setRun(data);
      toast(data.status === 'succeeded' ? '执行成功' : `执行结束：${data.status}`, data.status === 'succeeded' ? 'ok' : 'bad');
      onChanged();
    }).catch((exc: Error) => {
      setRun({ status: 'failed', error: { code: 'error', message: exc.message, details: {} }, node_runs: {} } as unknown as Run);
    }).finally(() => setBusy(false));
  };

  const dagNodes = run ? runNodes(run) : [];
  const runEdges = run ? (((run as unknown as { definition_snapshot?: { edges?: { source: string; target: string }[] } }).definition_snapshot)?.edges || []) : [];

  return (
    <>
      <Prompt note={<span className="muted">一次编排、反复复用；发布前强制校验，已发布版本不可覆盖</span>}>
        workflowhub ls workflows/
      </Prompt>

      <div className="grid2" style={{ gridTemplateColumns: '260px 1fr', alignItems: 'start' }}>
        <section>
          <SecCap title="VERSIONS" note={`${rows.length} 个`} />
          {rows.length === 0 ? <Box pad><span className="muted">还没有已发布版本。</span></Box> : (
            <Box>
              <table className="ls">
                <thead><tr><th>工作流</th><th style={{ width: 74 }}>节点</th></tr></thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={`${row.id}:${row.version}`} className={selected?.id === row.id && selected?.version === row.version ? 'sel' : ''}
                      onClick={() => { setSelected(row); setRun(null); setReport(null); }}>
                      <td><Fl>{row.id}</Fl>@{row.version}<div className="muted" style={{ fontSize: 11 }}>{row.name}</div></td>
                      <td>{row.nodes}/{row.edges}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Box>
          )}
          <div className="row tight" style={{ marginTop: 8 }}>
            <Btn small onClick={() => { setText(TEMPLATE); setSelected(null); setRun(null); }}>+ 载入模板</Btn>
            <Btn small variant="ghost" onClick={() => {
              const seed = JSON.parse(TEMPLATE) as Record<string, unknown>;
              const id = window.prompt('新的 workflow id（小写字母、数字、连字符）', 'my-flow');
              if (!id) return;
              seed.id = id;
              seed.name = window.prompt('显示名称', '我的流程') || id;
              setText(JSON.stringify(seed, null, 2)); setSelected(null); setRun(null);
              toast('已填入编辑器');
            }}>新建</Btn>
          </div>
        </section>

        <section style={{ display: 'grid', gap: 16, minWidth: 0 }}>
          <div>
            <SecCap title="CAT definition.json" note="可直接编辑" right={
              <span className="row tight">
                {busy && <Spinner />}
                <Btn small onClick={validate}>校验</Btn>
                <Btn small variant="primary" onClick={save} disabled={!parsed.doc}>发布版本</Btn>
              </span>} />
            {parsed.error && <div style={{ marginBottom: 8 }}><Note tone="bad">JSON 解析失败：{parsed.error}</Note></div>}
            <textarea className="textarea mono" rows={16} value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />
            {report && (
              <div style={{ marginTop: 8, display: 'grid', gap: 6 }}>
                {report.valid
                  ? <Note tone="info">校验通过{report.warnings.length ? `，${report.warnings.length} 条提示` : ''}</Note>
                  : <Note tone="bad">{report.errors.slice(0, 8).map((e) => <div key={e}>· {e}</div>)}</Note>}
                {report.warnings.map((w) => <div key={w} className="muted">提示：{w}</div>)}
              </div>
            )}
          </div>

          <div>
            <SecCap title="DESCRIBE --graph" note="按依赖自动分层" />
            {nodesOf(parsed.doc).length === 0 ? <Box pad><span className="muted">JSON 里还没有节点。</span></Box>
              : <Dag nodes={nodesOf(parsed.doc).map((n) => ({ id: n.id, skill: n.skill_id }))} edges={edgesOf(parsed.doc)} />}
          </div>

          <div>
            <SecCap title="RUN" note="节点输入支持 literal / workflow_input / node_output / context" />
            <div className="grid2" style={{ gridTemplateColumns: '1fr 200px', alignItems: 'start' }}>
              <Fieldish label="运行输入 JSON">
                <textarea className="textarea" rows={5} value={inputs} onChange={(e) => setInputs(e.target.value)} />
              </Fieldish>
              <Box pad>
                <div className="row" style={{ alignItems: 'flex-start' }}>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="muted" style={{ fontSize: 11 }}>目标</div>
                    <div>{selected ? `${selected.id}@${selected.version}` : '未选择'}</div>
                  </div>
                  <Btn variant="primary" onClick={execute} disabled={!selected || busy}>运行 ⏎</Btn>
                </div>
              </Box>
            </div>
          </div>

          {run && (
            <div>
              <SecCap title={`RESULT ${run.id.slice(0, 10)}`} note={<span className="muted">trace {run.trace_id.slice(0, 10)}</span>} />
              <div className="row tight" style={{ marginBottom: 8 }}>
                <StatusPill status={run.status} />
                <Pill>{Object.keys(run.node_runs).length} 节点</Pill>
                {run.error && <Pill tone="bad">{run.error.code}</Pill>}
              </div>
              {run.error && <div style={{ marginBottom: 8 }}><Note tone="bad">{run.error.message}</Note></div>}
              <Dag nodes={dagNodes} edges={runEdges} />
              <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
                {Object.values(run.node_runs).map((node) => (
                  <Box pad key={node.id}>
                    <div className="row tight">
                      <b>{node.node_id}</b>
                      <span className="muted">{node.skill_id}@{node.skill_version}</span>
                      <StatusPill status={node.status} />
                      {node.attempt > 1 && <Pill tone="req">retry {node.attempt}</Pill>}
                      {node.started_at && node.finished_at && (
                        <span className="right muted">{new Date(node.finished_at).getTime() - new Date(node.started_at).getTime()}ms</span>
                      )}
                    </div>
                    {node.result?.error && <div className="muted" style={{ color: 'var(--red)', marginTop: 4 }}>{node.result.error.message}</div>}
                    {node.result?.outputs && Object.keys(node.result.outputs).length > 0 && (
                      <pre className="code" style={{ maxHeight: 160 }}>{JSON.stringify(node.result.outputs, null, 2).slice(0, 2500)}</pre>
                    )}
                    {(node.result?.artifacts ?? []).map((a) => (
                      <a key={a.uri} className="fl" style={{ fontSize: 11.5 }}
                        href={`/api/v1/files?path=${encodeURIComponent(a.uri)}&download=true`}>↓ {a.uri.split(/[\\/]/).pop()}</a>
                    ))}
                  </Box>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>

      {rows.length === 0 && (
        <Empty title="还没有已发布的 workflow">
          点「+ 载入模板」生成第一个三段式流程；也可以把工具库里的命令 id 填进节点的 <b>skill_id</b>，
          参数写在 <b>inputs.argv</b> 里。
        </Empty>
      )}
    </>
  );
}

function Fieldish({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="field"><label>{label}</label>{children}</div>;
}
