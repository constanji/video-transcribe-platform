'use client';

export default function SettingsTabs({
  value,
  onChange,
}: {
  value: 'parser' | 'transcription' | 'templates' | 'services';
  onChange: (value: 'parser' | 'transcription' | 'templates' | 'services') => void;
}) {
  const tabs = [
    ['parser', '解析器配置'],
    ['transcription', '转录分析'],
    ['templates', '模板配置'],
    ['services', '服务配置'],
  ] as const;
  return (
    <div className="settings-tabs">
      {tabs.map(([id, label]) => (
        <button type="button" key={id} className={value === id ? 'active' : ''} onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}
