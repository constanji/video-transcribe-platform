'use client';

import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, SyntheticEvent } from 'react';

export type AppSelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

/**
 * 全站统一的自绘下拉选择器。
 *
 * 原生 <select> 的弹出列表由操作系统绘制（Windows 上是灰底高亮的系统白框，
 * 直角、字体与应用不一致，也无法定制），这里把触发器与列表都自绘，视觉与
 * 应用 10px 圆角体系一致。API 对齐原生 select：value + onChange + options。
 *
 * 空间处理：靠右自动右对齐、贴底自动向上弹，避免被容器/视口裁剪。
 */
export default function AppSelect({
  value,
  options,
  onChange,
  disabled = false,
  ariaLabel,
  placeholder = '请选择',
  className = '',
  minWidth = 120,
}: {
  value: string;
  options: AppSelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
  placeholder?: string;
  /** 附加到根元素，用于局部宽度/字号微调 */
  className?: string;
  /** 菜单最小宽度（px），触发器较窄时保证可读 */
  minWidth?: number;
}) {
  const [open, setOpen] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const current = options.find((item) => item.value === value);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent | TouchEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== 'Escape') return;
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

  // 打开时测量：右侧空间不足则右对齐，底部空间不足则向上弹。
  useEffect(() => {
    if (!open || !rootRef.current) return;
    const rect = rootRef.current.getBoundingClientRect();
    setAlignRight(rect.right + Math.max(minWidth, 220) > window.innerWidth);
    setDropUp(rect.bottom + 240 > window.innerHeight && rect.top > 260);
  }, [open, minWidth]);

  function step(direction: number) {
    const usable = options.filter((item) => !item.disabled);
    if (!usable.length) return;
    const index = usable.findIndex((item) => item.value === value);
    const next = usable[(index + direction + usable.length) % usable.length];
    onChange(next.value);
  }

  function onTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (open) step(event.key === 'ArrowDown' ? 1 : -1);
      else setOpen(true);
    }
    if (event.key === 'Enter' || event.key === ' ') {
      // label 包裹时原生行为会冒泡触发卡片点击等，这里统一截住。
      event.stopPropagation();
    }
  }

  // 吞掉组件内部的点击/按键冒泡（卡片类祖先有点击打开详情的行为）。
  const swallow = (event: SyntheticEvent) => event.stopPropagation();

  return (
    <div
      ref={rootRef}
      className={`app-select${open ? ' is-open' : ''}${disabled ? ' is-disabled' : ''} ${className}`}
      onKeyDown={swallow}
      onClick={swallow}
    >
      <button
        type="button"
        className="app-select-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((prev) => !prev);
        }}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="app-select-value">{current ? current.label : placeholder}</span>
        <svg className="app-select-caret" viewBox="0 0 24 24" aria-hidden="true">
          <path fill="currentColor" d="M12 15.6L5.4 9l1.4-1.4L12 12.8l5.2-5.2L18.6 9z" />
        </svg>
      </button>
      {open && (
        <div
          className={`app-select-menu${alignRight ? ' is-right' : ''}${dropUp ? ' is-up' : ''}`}
          role="listbox"
          aria-label={ariaLabel}
          style={minWidth ? { minWidth } : undefined}
        >
          {!options.length && <p className="app-select-empty">暂无可用选项</p>}
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
                <span className="app-select-option-label">{item.label}</span>
                {selected && (
                  <svg className="app-select-check" viewBox="0 0 24 24" aria-hidden="true">
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
