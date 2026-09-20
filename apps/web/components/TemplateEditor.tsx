'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { Template } from '../lib/types';

type SectionDraft = {
  title: string;
  instruction: string;
  format: string;
  item_format?: string;
};

type Draft = {
  name: string;
  description: string;
  sections: SectionDraft[];
};

const EMPTY_SECTION: SectionDraft = { title: '', instruction: '', format: 'paragraph' };

export const EMPTY_TEMPLATE: Draft = {
  name: '',
  description: '',
  sections: [{ title: '摘要', instruction: '总结关键内容。', format: 'paragraph' }],
};

function toDraft(template?: Template | null): Draft {
  if (!template) return { ...EMPTY_TEMPLATE, sections: EMPTY_TEMPLATE.sections.map((section) => ({ ...section })) };
  return {
    name: template.name,
    description: template.description,
    sections: (template.sections || []).map((section) => ({
      title: section.title,
      instruction: section.instruction,
      format: section.format || 'paragraph',
      item_format: section.item_format || '',
    })),
  };
}

export default function TemplateEditor({
  open,
  title,
  initial,
  onClose,
  onSaved,
  onToast,
}: {
  open: boolean;
  title: string;
  initial?: Template | null;
  onClose: () => void;
  onSaved: (template: Template) => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [draft, setDraft] = useState<Draft>(toDraft(initial));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) setDraft(toDraft(initial));
  }, [open, initial]);

  if (!open) return null;

  function updateSection(index: number, patch: Partial<SectionDraft>) {
    setDraft((current) => ({
      ...current,
      sections: current.sections.map((section, sectionIndex) => (
        sectionIndex === index ? { ...section, ...patch } : section
      )),
    }));
  }

  async function save() {
    if (!draft.name.trim() || !draft.description.trim()) {
      onToast('请填写模板名称和说明', 'error');
      return;
    }
    if (draft.sections.some((section) => !section.title.trim() || !section.instruction.trim())) {
      onToast('每个章节都需要标题和生成说明', 'error');
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: draft.name.trim(),
        description: draft.description.trim(),
        sections: draft.sections.map((section) => ({
          title: section.title.trim(),
          instruction: section.instruction.trim(),
          format: section.format,
          item_format: section.item_format?.trim() || undefined,
        })),
        show_on_home: initial?.show_on_home ?? true,
      };
      const saved = initial
        ? await api<Template>(`/api/templates/${initial.id}`, { method: 'PUT', body: JSON.stringify(payload) })
        : await api<Template>('/api/templates', { method: 'POST', body: JSON.stringify(payload) });
      onSaved(saved);
      onToast(initial ? '模板已保存' : '模板已创建');
      onClose();
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="confirm-mask" onClick={onClose}>
      <div className="template-editor" onClick={(event) => event.stopPropagation()}>
        <h3>{title}</h3>
        <p>名称、说明和章节会用于生成总结。</p>
        <label>
          模板名称
          <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如：每周项目同步" />
        </label>
        <label>
          说明
          <textarea value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="说明这个模板的用途" />
        </label>
        <div className="template-editor-head">
          <strong>章节</strong>
          <button
            type="button"
            className="text-button"
            onClick={() => setDraft((current) => ({ ...current, sections: [...current.sections, { ...EMPTY_SECTION }] }))}
          >
            添加章节
          </button>
        </div>
        {draft.sections.map((section, index) => (
          <div className="template-section" key={index}>
            <div className="template-section-head">
              <span>章节 {index + 1}</span>
              {draft.sections.length > 1 && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setDraft((current) => ({
                    ...current,
                    sections: current.sections.filter((_, sectionIndex) => sectionIndex !== index),
                  }))}
                >
                  移除
                </button>
              )}
            </div>
            <div className="template-section-grid">
              <label>
                标题
                <input value={section.title} onChange={(event) => updateSection(index, { title: event.target.value })} />
              </label>
              <label>
                格式
                <select value={section.format} onChange={(event) => updateSection(index, { format: event.target.value })}>
                  <option value="paragraph">段落</option>
                  <option value="list">列表</option>
                  <option value="string">单行文本</option>
                </select>
              </label>
            </div>
            <label>
              生成说明
              <textarea value={section.instruction} onChange={(event) => updateSection(index, { instruction: event.target.value })} placeholder="说明 AI 应在该章节提取的内容" />
            </label>
          </div>
        ))}
        <div className="template-editor-actions">
          <button type="button" className="outline-button" onClick={onClose}>取消</button>
          <button type="button" className="primary-button" disabled={saving} onClick={save}>
            {saving ? '保存中…' : '保存模板'}
          </button>
        </div>
      </div>
    </div>
  );
}
