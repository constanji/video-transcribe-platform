'use client';

import { useState } from 'react';
import { api } from '../lib/api';
import type { Template } from '../lib/types';
import TemplateEditor from './TemplateEditor';

export default function TemplateSettings({
  templates,
  onTemplates,
  onToast,
}: {
  templates: Template[];
  onTemplates: (items: Template[]) => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [editing, setEditing] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<Template | null>(null);

  function replace(saved: Template) {
    const exists = templates.some((item) => item.id === saved.id);
    onTemplates(exists ? templates.map((item) => (item.id === saved.id ? saved : item)) : [saved, ...templates]);
  }

  async function toggleHome(item: Template) {
    const saved = await api<Template>(`/api/templates/${item.id}`, {
      method: 'PUT',
      body: JSON.stringify({ show_on_home: !item.show_on_home }),
    });
    replace(saved);
  }

  async function duplicate(item: Template) {
    const saved = await api<Template>('/api/templates', {
      method: 'POST',
      body: JSON.stringify({
        name: `${item.name} 副本`,
        description: item.description,
        sections: item.sections,
        show_on_home: false,
      }),
    });
    replace(saved);
    onToast('已复制模板');
  }

  async function restore(item: Template) {
    if (!window.confirm(`恢复「${item.name}」到内置原始版本？当前修改会丢失。`)) return;
    const saved = await api<Template>(`/api/templates/${item.id}/restore`, { method: 'POST' });
    replace(saved);
    onToast('已恢复内置模板');
  }

  async function remove(item: Template) {
    if (!window.confirm(`确定删除「${item.name}」？此操作无法撤销。`)) return;
    await api(`/api/templates/${item.id}`, { method: 'DELETE' });
    onTemplates(templates.filter((entry) => entry.id !== item.id));
    onToast('模板已删除');
  }

  return (
    <>
      <div className="template-config-head">
        <div>
          <h2>模板配置</h2>
          <p className="settings-intro">增删改总结模板。打开「首页显示」后，才会出现在工作台的模板列表里。</p>
        </div>
        <button
          type="button"
          className="primary-button"
          onClick={() => { setEditingTemplate(null); setEditing(true); }}
        >
          增加模板
        </button>
      </div>
      {templates.length === 0 && <p className="settings-hint">还没有模板，请先新建一个。</p>}
      {templates.map((item) => (
        <article className="template-card" key={item.id}>
          <div className="template-card-top">
            <div>
              <div className="template-card-title">
                <h3>{item.name}</h3>
                <em>{item.origin === 'builtin' ? '内置' : '自定义'}</em>
              </div>
              <p>{item.description || '暂无说明'}</p>
            </div>
            <label className="template-switch">
              <input type="checkbox" checked={item.show_on_home !== false} onChange={() => toggleHome(item)} />
              首页显示
            </label>
          </div>
          <div className="template-card-actions">
            <button type="button" className="outline-button" onClick={() => { setEditingTemplate(item); setEditing(true); }}>编辑</button>
            <button type="button" className="outline-button" onClick={() => duplicate(item)}>复制</button>
            {item.origin === 'builtin' ? (
              <button type="button" className="outline-button" onClick={() => restore(item)}>恢复默认</button>
            ) : (
              <button type="button" className="outline-button" onClick={() => remove(item)}>删除</button>
            )}
          </div>
        </article>
      ))}
      <TemplateEditor
        open={editing}
        title={editingTemplate ? '编辑模板' : '增加模板'}
        initial={editingTemplate}
        onClose={() => { setEditing(false); setEditingTemplate(null); }}
        onSaved={replace}
        onToast={onToast}
      />
    </>
  );
}
