-- MVP relational target. Current runnable skeleton uses versioned JSON repositories.
PRAGMA foreign_keys = ON;

CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE skill_versions (
  skill_id TEXT NOT NULL REFERENCES skills(id),
  version TEXT NOT NULL,
  contract_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  published_at TEXT NOT NULL,
  PRIMARY KEY (skill_id, version)
);
CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE workflow_versions (
  workflow_id TEXT NOT NULL REFERENCES workflows(id),
  version INTEGER NOT NULL CHECK (version >= 1),
  definition_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  published_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, version)
);
CREATE TABLE workflow_nodes (
  workflow_id TEXT NOT NULL,
  workflow_version INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  node_type TEXT NOT NULL,
  skill_id TEXT,
  skill_version TEXT,
  config_json TEXT NOT NULL,
  PRIMARY KEY (workflow_id, workflow_version, node_id),
  FOREIGN KEY (workflow_id, workflow_version) REFERENCES workflow_versions(workflow_id, version),
  FOREIGN KEY (skill_id, skill_version) REFERENCES skill_versions(skill_id, version)
);
CREATE TABLE workflow_edges (
  workflow_id TEXT NOT NULL,
  workflow_version INTEGER NOT NULL,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  condition_json TEXT,
  PRIMARY KEY (workflow_id, workflow_version, source_node_id, target_node_id),
  FOREIGN KEY (workflow_id, workflow_version, source_node_id) REFERENCES workflow_nodes(workflow_id, workflow_version, node_id),
  FOREIGN KEY (workflow_id, workflow_version, target_node_id) REFERENCES workflow_nodes(workflow_id, workflow_version, node_id)
);
CREATE TABLE workflow_runs (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  workflow_version INTEGER NOT NULL,
  definition_snapshot_json TEXT NOT NULL,
  status TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  outputs_json TEXT NOT NULL,
  error_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY (workflow_id, workflow_version) REFERENCES workflow_versions(workflow_id, version)
);
CREATE TABLE node_runs (
  id TEXT PRIMARY KEY,
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
  node_id TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  skill_version TEXT NOT NULL,
  skill_snapshot_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  inputs_json TEXT NOT NULL,
  result_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY (skill_id, skill_version) REFERENCES skill_versions(skill_id, version)
);
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  node_run_id TEXT NOT NULL REFERENCES node_runs(id),
  uri TEXT NOT NULL,
  media_type TEXT NOT NULL,
  size_bytes INTEGER
);
CREATE TABLE review_tasks (
  id TEXT PRIMARY KEY,
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
  node_run_id TEXT NOT NULL REFERENCES node_runs(id),
  payload_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  status TEXT NOT NULL,
  reviewer TEXT,
  decision TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX idx_workflow_runs_version ON workflow_runs(workflow_id, workflow_version);
CREATE INDEX idx_node_runs_run ON node_runs(workflow_run_id);
CREATE INDEX idx_review_tasks_status ON review_tasks(status);
