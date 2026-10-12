'use client';

export default function SettingsTabs({
  value,
  onChange,
  showUsers = false,
}: {
  value: 'parser' | 'transcription' | 'templates' | 'services' | 'users';
  onChange: (value: 'parser' | 'transcription' | 'templates' | 'services' | 'users') => void;
  showUsers?: boolean;
}) {
  const tabs: [('parser' | 'transcription' | 'templates' | 'services' | 'users'), string][] = [
    ['parser', '解析器配置'],
    ['transcription', '转录分析'],
    ['templates', '模板配置'],
    ['services', '服务配置'],
  ];
  if (showUsers) tabs.push(['users', '用户管理']);
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
