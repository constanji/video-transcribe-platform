'use client';

import { useEffect, useRef, useState } from 'react';

export type StudioSelectOption = {
  value: string;
  label: string;
  /** 次要说明，例如「未配置」，单独一行弱化显示 */
  hint?: string;
  disabled?: boolean;
};

/**
 * 工具栏里的下拉选择器。
 *
 * 用自绘列表替换原生 <select>：原生控件的弹出列表由系统绘制，无法跟随应用的
 * 排版与配色（在 Windows 上就是一个系统样式的白框），选项文字也会被 select 的
 * max-width 硬裁掉。这里把触发器和列表都收进 .studio-tool-select，
 * 三者（生成 / 语言 / 模型 / 模板）因此能保持同一套高度、圆角和字号。
 *
 * 祖先链（.studio-card / .studio-body / .studio-pane / .studio-pane-toolbar）
 * 都是 overflow:visible，所以列表用绝对定位即可，不需要 portal。
 */
export default function StudioSelect({
  icon,
  value,
  options,
  placeholder = '请选择',
  ariaLabel,
  title,
  disabled = false,
  muted = false,
  maxWidth,
  onChange,
}: {
  icon: string;
  value: string;
  options: StudioSelectOption[];
  placeholder?: string;
  ariaLabel: string;
  title?: string;
  disabled?: boolean;
  muted?: boolean;
  maxWidth?: number;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const current = options.find((item) => item.value === value);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent | TouchEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      // TranscriptStudio 也在 window 上监听 Escape 关闭整个对话框。这里在
      // document 阶段截住冒泡，按下 Escape 只收起下拉，不会连弹窗一起关掉。
      event.stopPropagation();
      event.preventDefault();
      setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // 靠右的控件把列表左对齐会顶出卡片，这里按需改成右对齐。
  useEffect(() => {
    if (!open || !rootRef.current) return;
    const rect = rootRef.current.getBoundingClientRect();
    setAlignRight(rect.right + 260 > window.innerWidth);
  }, [open]);

  function step(direction: number) {
    const usable = options.filter((item) => !item.disabled);
    if (!usable.length) return;
    const index = usable.findIndex((item) => item.value === value);
    const next = usable[(index + direction + usable.length) % usable.length];
    onChange(next.value);
  }

  return (
    <div
      ref={rootRef}
      className={`studio-tool studio-tool-select${open ? ' is-open' : ''}${disabled ? ' is-disabled' : ''}${muted ? ' muted-select' : ''}`}
      title={title}
    >
      <svg className="studio-select-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path fill="currentColor" d={icon} />
      </svg>
      <button
        type="button"
        className="studio-select-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          if (open) step(event.key === 'ArrowDown' ? 1 : -1);
          else setOpen(true);
        }}
      >
        <span className="studio-select-value" style={maxWidth ? { maxWidth } : undefined}>
          {current ? current.label : placeholder}
        </span>
        <svg className="studio-select-caret" viewBox="0 0 24 24" aria-hidden="true">
          <path fill="currentColor" d="M12 15.6L5.4 9l1.4-1.4L12 12.8l5.2-5.2L18.6 9z" />
        </svg>
      </button>
      {open && (
        <div
          className={`studio-select-menu${alignRight ? ' is-right' : ''}`}
          role="listbox"
          aria-label={ariaLabel}
        >
          {!options.length && <p className="studio-select-empty">暂无可用选项</p>}
          {options.map((item) => {
            const selected = item.value === value;
            return (
              <button
                key={item.value}
                type="button"
                role="option"
                aria-selected={selected}
                disabled={item.disabled}
                className={selected ? 'is-selected' : undefined}
                onClick={() => {
                  onChange(item.value);
                  setOpen(false);
                }}
              >
                <span className="studio-select-option">
                  <span className="studio-select-option-label">{item.label}</span>
                  {item.hint && <small>{item.hint}</small>}
                </span>
                {selected && (
                  <svg className="studio-select-check" viewBox="0 0 24 24" aria-hidden="true">
                    <path fill="currentColor" d="M9.6 16.4l-4.8-4.8 1.4-1.4 3.4 3.4 8-8 1.4 1.4z" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
