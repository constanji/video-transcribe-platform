// 极简 Markdown → HTML 渲染器，只覆盖总结模型实际会输出的语法
// （标题 / 无序有序列表 / 引用 / 分割线 / 粗体 / 斜体 / 行内代码 / 链接 / 段落）。
//
// 故意不引第三方库：apps/web 目前零运行时依赖，这台机器上 npm 拉包也不可靠；
// 而且先做 HTML 转义、再转换语法，模型输出里的尖括号和引号都变不成标签，
// 天然没有注入问题。

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

// 链接只放行 http(s) 与 mailto，避免 javascript: 之类的伪协议。
function safeHref(url: string) {
  const trimmed = url.trim();
  return /^(https?:\/\/|mailto:)/i.test(trimmed) ? trimmed : '';
}

// 行内语法。入参必须是「已经转义过」的文本，否则会把用户输入当标签注入。
function inline(text: string) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_all, label: string, href: string) => {
      const target = safeHref(href);
      return target
        ? `<a href="${target}" target="_blank" rel="noopener noreferrer">${label}</a>`
        : label;
    });
}

export function renderMarkdown(markdown: string) {
  const lines = escapeHtml((markdown || '').replace(/\r\n/g, '\n')).split('\n');
  const out: string[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushList = () => {
    const current = list;
    if (!current) return;
    const tag = current.ordered ? 'ol' : 'ul';
    out.push(`<${tag}>${current.items.map((item) => `<li>${item}</li>`).join('')}</${tag}>`);
    list = null;
  };
  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
    paragraph = [];
  };
  const flushAll = () => {
    flushList();
    flushParagraph();
  };

  for (const raw of lines) {
    const trimmed = raw.trim();
    if (!trimmed) {
      flushAll();
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushAll();
      out.push('<hr>');
      continue;
    }

    // '>' 在转义后变成 &gt;。
    const quote = trimmed.match(/^&gt;\s?(.*)$/);
    if (quote) {
      flushAll();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*+]\s+(.*)$/);
    if (bullet) {
      flushParagraph();
      if (!list || list.ordered) list = { ordered: false, items: [] };
      list.items.push(inline(bullet[1]));
      continue;
    }

    const ordered = trimmed.match(/^\d{1,3}[.)]\s+(.*)$/);
    if (ordered) {
      flushParagraph();
      if (!list || !list.ordered) list = { ordered: true, items: [] };
      list.items.push(inline(ordered[1]));
      continue;
    }

    flushList();
    paragraph.push(trimmed);
  }
  flushAll();
  return out.join('\n');
}
