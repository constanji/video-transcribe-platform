'use client';

import { useEffect, useRef, useState } from 'react';
import type { Template } from '../lib/types';

export default function TemplatePicker({
  templates,
  value,
  onChange,
  onAdd,
  compact = false,
}: {
  templates: Template[];
  value: string;
  onChange: (id: string) => void;
  onAdd: () => void;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = templates.find((item) => item.id === value);

  useEffect(() => {
    function onPointer(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, []);

  return (
    <div className={`template-picker ${compact ? 'compact' : ''}`} ref={rootRef}>
      <button type="button" className="template-picker-trigger" onClick={() => setOpen((current) => !current)}>
        <span>
          <strong>{selected?.name || '请选择模板'}</strong>
          {!compact && selected?.description && <small>{selected.description}</small>}
        </span>
      </button>
      {open && (
        <div className="template-menu">
          <ul>
            {templates.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className={item.id === value ? 'selected' : ''}
                  onClick={() => {
                    onChange(item.id);
                    setOpen(false);
                  }}
                >
                  <strong>{item.name}</strong>
                  {item.description && <small>{item.description}</small>}
                </button>
              </li>
            ))}
            {templates.length === 0 && <li className="template-empty">暂无首页模板，请到设置中打开「首页显示」</li>}
          </ul>
          <button
            type="button"
            className="template-add"
            onClick={() => {
              setOpen(false);
              onAdd();
            }}
          >
            增加模板
          </button>
        </div>
      )}
    </div>
  );
}
