'use client';

const ADAPTERS = [
  { id: 'bilibili', name: 'B站解析器', extra: ['video_quality'] },
  { id: 'douyin', name: '抖音解析器' },
  { id: 'kuaishou', name: '快手解析器' },
  { id: 'weibo', name: '微博解析器' },
  { id: 'xhs', name: '小红书解析器' },
  { id: 'xiaoheihe', name: '小黑盒解析器' },
  { id: 'youtube', name: 'YouTube 解析器' },
  { id: 'tiktok', name: 'TikTok 解析器' },
  { id: 'twitter', name: 'Twitter 解析器' },
  { id: 'pixiv', name: 'Pixiv 解析器' },
  { id: 'shipinhao', name: '微信视频号解析器' },
  { id: 'qzone', name: 'QQ 空间解析器' },
];

const DEFAULTS = {
  log_level: 'info',
  enable_proxy: false,
  proxy: '',
  source_max_size: 90,
  source_max_minute: 15,
  download_timeout: 280,
  common_timeout: 15,
  download_retry_times: 2,
  temp_retain_hours: 24,
  clean_cron: '30 2 * * *',
  parsers: {} as Record<string, any>,
};

function parserDefault(id: string) {
  return {
    enable: true,
    use_proxy: ['youtube', 'tiktok', 'twitter'].includes(id),
    cookies: '',
    cookies_masked: '',
    video_quality: id === 'bilibili' ? '_720P' : '',
  };
}

export default function ParserSettings({
  value,
  onChange,
}: {
  value: any;
  onChange: (value: any) => void;
}) {
  const data = { ...DEFAULTS, ...value, parsers: { ...DEFAULTS.parsers, ...(value.parsers || {}) } };

  function patch(partial: Record<string, unknown>) {
    onChange({ ...data, ...partial });
  }

  function patchParser(id: string, partial: Record<string, unknown>) {
    const current = { ...parserDefault(id), ...(data.parsers[id] || {}) };
    patch({ parsers: { ...data.parsers, [id]: { ...current, ...partial } } });
  }

  return (
    <>
      <h2>解析器配置</h2>
      <p className="settings-intro">控制 Web 端链接解析、下载限制、代理和各平台 Cookie。</p>
      <h3 className="subheading">全局解析行为</h3>
      <label className="setting-row">
        <span><strong>日志级别</strong><small>影响解析与下载调试输出</small></span>
        <select value={data.log_level} onChange={(e) => patch({ log_level: e.target.value })}>
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
        </select>
      </label>
      <label className="setting-row">
        <span><strong>启用代理</strong><small>需要访问海外平台时打开</small></span>
        <input type="checkbox" checked={data.enable_proxy} onChange={(e) => patch({ enable_proxy: e.target.checked })} />
      </label>
      <label className="setting-row">
        <span><strong>代理地址</strong><small>例如 http://127.0.0.1:7890</small></span>
        <input value={data.proxy} onChange={(e) => patch({ proxy: e.target.value })} placeholder="留空则直连" />
      </label>
      <h3 className="subheading">下载限制</h3>
      {[
        ['source_max_size', '最大资源大小（MB）', 90],
        ['source_max_minute', '最大资源时长（分钟）', 15],
        ['download_timeout', '下载请求超时（秒）', 280],
        ['common_timeout', '普通请求超时（秒）', 15],
        ['download_retry_times', '下载失败重试次数', 2],
        ['temp_retain_hours', '临时文件保留时间（小时）', 24],
      ].map(([key, label]) => (
        <label className="setting-row" key={String(key)}>
          <span><strong>{label}</strong><small>超过限制的媒体不会下载到本地临时目录</small></span>
          <input type="number" value={data[key as string]} onChange={(e) => patch({ [key as string]: Number(e.target.value) })} />
        </label>
      ))}
      <label className="setting-row">
        <span><strong>自动清理周期</strong><small>Cron，留空表示禁用</small></span>
        <input value={data.clean_cron} onChange={(e) => patch({ clean_cron: e.target.value })} />
      </label>
      <h3 className="subheading">解析器列表</h3>
      {ADAPTERS.map((adapter) => {
        const item = { ...parserDefault(adapter.id), ...(data.parsers[adapter.id] || {}) };
        return (
          <details className="adapter-block" key={adapter.id}>
            <summary>
              <span className="adapter-name">{adapter.name}</span>
              <label className="adapter-enable" onClick={(e) => e.stopPropagation()}>
                启用
                <input type="checkbox" checked={item.enable} onChange={(e) => patchParser(adapter.id, { enable: e.target.checked })} />
              </label>
            </summary>
            <div className="adapter-body">
              <label className="adapter-field">
                <span>
                  <strong>使用代理</strong>
                  <small>该平台请求走上方配置的全局代理</small>
                </span>
                <input type="checkbox" checked={item.use_proxy} onChange={(e) => patchParser(adapter.id, { use_proxy: e.target.checked })} />
              </label>
              <label className="adapter-field adapter-field-stack">
                <span>
                  <strong>Cookie / 凭证</strong>
                  <small>{item.cookies_masked ? '已保存，重新填写将覆盖原值' : '仅保存在服务端，保存后脱敏展示'}</small>
                </span>
                <textarea
                  rows={4}
                  value={item.cookies}
                  onChange={(e) => patchParser(adapter.id, { cookies: e.target.value })}
                  placeholder={item.cookies_masked || '选填'}
                />
              </label>
              {adapter.id === 'bilibili' && (
                <label className="adapter-field">
                  <span>
                    <strong>默认分辨率</strong>
                    <small>高画质可能需要登录 Cookie</small>
                  </span>
                  <select value={item.video_quality} onChange={(e) => patchParser(adapter.id, { video_quality: e.target.value })}>
                    {['_360P', '_480P', '_720P', '_1080P', '_4K'].map((q) => <option key={q} value={q}>{q.replace('_', '')}</option>)}
                  </select>
                </label>
              )}
              <div className="adapter-footer">
                <button type="button" className="outline-button" onClick={() => patchParser(adapter.id, parserDefault(adapter.id))}>恢复默认值</button>
              </div>
            </div>
          </details>
        );
      })}
    </>
  );
}

export const PARSER_DEFAULTS = DEFAULTS;
