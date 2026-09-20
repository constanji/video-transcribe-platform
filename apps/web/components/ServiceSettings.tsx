'use client';

export default function ServiceSettings({ value, onChange }: { value: any; onChange: (value: any) => void }) {
  const data = value || {};
  function patch(partial: Record<string, unknown>) {
    onChange({ ...data, ...partial });
  }
  return (
    <>
      <h2>服务配置</h2>
      <p className="settings-intro">管理 API 地址、存储目录和任务清理策略。</p>
      <label className="setting-row"><span><strong>Web API 地址</strong></span><input value={data.web_api_url || ''} onChange={(e) => patch({ web_api_url: e.target.value })} placeholder="http://localhost:8000" /></label>
      <label className="setting-row"><span><strong>MOSS 服务地址</strong></span><input value={data.moss_url || ''} onChange={(e) => patch({ moss_url: e.target.value })} placeholder="http://localhost:9000" /></label>
      <label className="setting-row"><span><strong>存储目录</strong></span><input value={data.storage_root || ''} onChange={(e) => patch({ storage_root: e.target.value })} /></label>
      <label className="setting-row"><span><strong>临时文件目录</strong></span><input value={data.temp_dir || ''} onChange={(e) => patch({ temp_dir: e.target.value })} /></label>
      <label className="setting-row"><span><strong>封面缓存目录</strong></span><input value={data.cover_dir || ''} onChange={(e) => patch({ cover_dir: e.target.value })} /></label>
      <label className="setting-row"><span><strong>任务保留天数</strong></span><input type="number" value={data.retention_days || 30} onChange={(e) => patch({ retention_days: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>启用自动清理</strong></span><input type="checkbox" checked={data.auto_clean !== false} onChange={(e) => patch({ auto_clean: e.target.checked })} /></label>
      <div className="health-box">
        <span className="health-dot" /> PostgreSQL / SQLite 已连接
        <span className="health-dot" /> 当前版本 0.1.0
        <span className="health-dot" /> 数据库表已自动迁移
      </div>
    </>
  );
}
