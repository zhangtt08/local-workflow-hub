import { useCallback, useState } from 'react';
import { api, type AgentInfo } from '../hub';
import { Box, Btn, CopyBlock, Fl, Log, LogLine, Note, Pill, Prompt, SecCap, Spinner, useAsync } from '../ui';

type Probe = { label: string; path: string; ok: boolean | null; ms: number | null; note: string };

const PROBES: Omit<Probe, 'ok' | 'ms' | 'note'>[] = [
  { label: '存活检查', path: '/health' },
  { label: '目录概况', path: '/api/v1/overview' },
  { label: '工具列表', path: '/api/v1/tools?limit=5' },
  { label: 'Agent 元信息', path: '/api/v1/agent/info' },
  { label: 'Function schema', path: '/api/v1/tools.json' },
  { label: '旧契约 health', path: '/api/health' },
  { label: '旧契约 tools', path: '/api/agent/tools?limit=2' },
  { label: '旧契约 manifest', path: '/api/agent/manifest' },
  { label: 'OpenAPI', path: '/openapi.json' },
];

export default function ConnectView() {
  const info = useAsync(() => api.agentInfo(), []);
  const mcp = useAsync(() => api.mcpConfig(), []);
  const guide = useAsync(() => api.guide(), []);
  const [results, setResults] = useState<Probe[]>(PROBES.map((p) => ({ ...p, ok: null, ms: null, note: '' })));
  const [testing, setTesting] = useState(false);
  const [showGuide, setShowGuide] = useState(false);

  const test = useCallback(async () => {
    setTesting(true);
    const next: Probe[] = [];
    for (const probe of PROBES) {
      const started = performance.now();
      try {
        const response = await fetch(probe.path);
        const body = await response.text();
        let note = `${response.status} · ${body.length}B`;
        const json = body.startsWith('{') ? (JSON.parse(body) as Record<string, number>) : {};
        if (probe.path.endsWith('tools.json')) note = `${json.count ?? 0} 个工具 schema`;
        else if (probe.path.endsWith('overview')) note = `可用 ${(JSON.parse(body) as { catalog?: { enabled?: number } }).catalog?.enabled ?? 0} 个工具`;
        else if (probe.path.includes('/api/agent/tools')) note = `目录 ${json.count_total ?? 0} 条 · 本页 ${json.returned ?? 0} 条`;
        next.push({ ...probe, ok: response.ok, ms: Math.round(performance.now() - started), note });
      } catch (exc) {
        next.push({ ...probe, ok: false, ms: null, note: (exc as Error).message });
      }
    }
    setResults(next);
    setTesting(false);
  }, []);

  if (info.error) return <Note tone="bad">{info.error}</Note>;
  if (!info.data) return <Spinner label="读取 Agent 元信息…" />;
  const data = info.data as AgentInfo;
  const bad = results.filter((r) => r.ok === false).length;

  const sections = (guide.data || '').split('\n## ').map((chunk, i) => {
    const lines = chunk.split('\n');
    const title = (i === 0 ? lines[0].replace(/^#\s*/, '') : lines[0]).replace(/^[\d.]+\s*/, '');
    return { title, body: lines.slice(1).join('\n').trim() };
  });

  return (
    <>
      <Prompt note={<span className="muted">{data.counts.enabled} 个可用工具，五种接法指向同一批能力</span>}>
        workflowhub agent <Fl>--how-to-connect</Fl>
      </Prompt>

      <div className="grid2">
        <Box pad>
          <div className="h1">服务地址</div>
          <div className="row tight" style={{ marginTop: 6 }}>
            <span className="fl">{data.base_url}</span>
            <Btn small variant="ghost" onClick={() => void navigator.clipboard.writeText(data.base_url).catch(() => undefined)}>复制</Btn>
            <span className="right"><Btn small variant="primary" onClick={() => void test()} disabled={testing}>测试连接{testing && <Spinner />}</Btn></span>
          </div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 8, display: 'grid', gap: 3 }}>
            <div>protocol  {data.protocol.join(' · ')}</div>
            <div>auth      {data.auth.required ? `需要请求头 ${data.auth.header}` : '本机直连（只监听 127.0.0.1）'}</div>
            <div>counts    条目 {data.counts.catalogue} · 可用 {data.counts.enabled} · skill {data.counts.handwritten_skills} · workflow {data.counts.workflows}</div>
            <div>risk      {data.risk.high_risk_enabled ? '高风险工具允许调用' : '高风险工具已整体关闭'} · {data.risk.approval_mode ? '审批模式开' : '审批模式关'}</div>
          </div>
          {results.some((r) => r.ok !== null) && (
            <Log style={{ marginTop: 10 }}>
              <LogLine level={bad === 0 ? 'ok' : 'err'}>
                {bad === 0 ? '所有端点连通' : `${bad} 个端点异常`} <Fl>-- self test</Fl>
              </LogLine>
              {results.filter((r) => r.ok !== null).map((r) => (
                <LogLine key={r.path} ts={`${r.ms ?? '—'}ms`} level={r.ok ? 'ok' : 'err'}>
                  <span className="muted">{r.label}</span> <span className="path">{r.path}</span> <span className="muted">· {r.note}</span>
                </LogLine>
              ))}
            </Log>
          )}
        </Box>

        <Box pad>
          <div className="h1">方式一 · MCP（推荐）</div>
          <div className="soft" style={{ fontSize: 12, margin: '4px 0 8px' }}>
            Agent 以子进程连上 WorkflowHub，先 <b>search_tools</b> 再 <b>invoke_tool</b>；
            默认只暴露 8 个元工具，避免上千条 schema 挤爆上下文。
          </div>
          <CopyBlock text={JSON.stringify(mcp.data ?? {}, null, 2)} label="mcpServers 配置" height={170} />
        </Box>
      </div>

      <div className="grid2">
        <Box pad>
          <div className="h1">方式二 · HTTP 直接调用</div>
          <div style={{ height: 8 }} />
          <CopyBlock label="搜索" text={`curl -s "${data.base_url}/api/v1/tools?query=ffmpeg&limit=5"`} height={70} />
          <div style={{ height: 8 }} />
          <CopyBlock label="调用（先 dry_run 预览命令行）"
            text={`curl -s -X POST "${data.base_url}/api/v1/tools/cli-ffmpeg/invoke" -H "Content-Type: application/json" -d '{"inputs":{"argv":["-version"],"dry_run":true}}'`} height={110} />
        </Box>
        <Box pad>
          <div className="h1">方式三 · Function calling</div>
          <div className="soft" style={{ fontSize: 12, margin: '4px 0 8px' }}>
            <span className="mono">GET /api/v1/tools.json</span> 直接返回 tools 数组；
            <span className="mono">function.name</span> 把下划线换回连字符就是 key。
          </div>
          <div className="row tight">
            <Pill tone="free">{data.counts.enabled} 个工具</Pill>
            <a className="btn sm" href="/api/v1/tools.json" download="workflowhub-tools.json">↓ 下载 tools.json</a>
            <Btn small variant="ghost" onClick={() => void fetch('/api/v1/tools.json').then((r) => r.text())
              .then((t) => navigator.clipboard.writeText(t)).catch(() => undefined)}>复制完整 schema</Btn>
          </div>
          <div style={{ height: 8 }} />
          <CopyBlock text={JSON.stringify({
            type: 'function',
            function: {
              name: 'cli_ffmpeg',
              description: '[high] ffmpeg — 视频/音频转码命令行',
              parameters: { type: 'object', properties: { argv: { type: 'array', items: { type: 'string' } }, dry_run: { type: 'boolean' } } },
            },
          }, null, 2)} label="结构示例" height={150} />
        </Box>
      </div>

      <Box pad>
        <div className="h1">方式四 · 命令行（没有 HTTP 客户端时）</div>
        <div className="soft" style={{ fontSize: 12, margin: '4px 0 8px' }}>CLI 与 HTTP/MCP 走同一套服务函数，行为完全一致。</div>
        <CopyBlock height={170} text={[
          'python -m workflowhub scan                      # 重新扫描本机',
          'python -m workflowhub tools ffmpeg --limit 5    # 搜索工具',
          'python -m workflowhub invoke cli-python --argv --version --dry-run',
          'python -m workflowhub mcp                       # 作为 MCP STDIO 服务',
          'python -m workflowhub doctor                    # 环境自检',
          'WorkflowHub.exe --mcp                           # 桌面版直接当 MCP 用',
        ].join('\n')} />
      </Box>

      <Box pad>
        <div className="h1">方式五 · 旧契约四端点（给现成的 Agent 配置）</div>
        <div className="soft" style={{ fontSize: 12, margin: '4px 0 8px' }}>
          其余项目用的都是 <span className="mono">/api/health · /api/agent/tools · /api/agent/manifest · POST /api/agent/tool</span>
          （<span className="mono">{'{ok, data}'}</span> 信封）。这里同一批工具也按那个形状说话，
          <b>风险闸门不放宽</b>：未启用 403、审批模式 409；差别只有两处 —— tools 是分页视图（默认 200 条），
          命令退出码非 0 时回 <span className="mono">200 + ok:false</span>，免得客户端把它当传输错误重发已有副作用的命令。
        </div>
        <CopyBlock label="列出工具（分页）" text={`curl -s "${data.base_url}/api/agent/tools?limit=50&offset=0"`} height={70} />
        <div style={{ height: 8 }} />
        <CopyBlock label="调用（tool 可以写 key，也可以写 function 名）"
          text={`curl -s -X POST "${data.base_url}/api/agent/tool" -H "Content-Type: application/json" -d '{"tool":"cli_ffmpeg","input":{"argv":["-version"],"dry_run":true},"actor":"my-agent"}'`} height={110} />
      </Box>

      <section>
        <SecCap title="CAT docs/agent-guide.md" note={`${Math.ceil((guide.data || '').length / 1000)}k 字`}
          right={<Btn small variant="ghost" onClick={() => setShowGuide(!showGuide)}>{showGuide ? '收起' : '展开'}</Btn>} />
        {showGuide ? sections.map((s) => (
          <div key={s.title} style={{ marginBottom: 12 }}>
            <div className="h1" style={{ fontSize: 13, marginBottom: 4 }}>{s.title}</div>
            <pre className="code" style={{ whiteSpace: 'pre-wrap', maxHeight: 420 }}>{s.body}</pre>
          </div>
        )) : <Note>指南含：invoke 字段表、错误格式、审批流程、安全约定、常见坑（中文名/空格路径、GBK 乱码、找不到工具）。</Note>}
      </section>
    </>
  );
}
