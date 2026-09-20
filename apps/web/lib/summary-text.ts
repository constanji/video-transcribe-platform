const THINK_RE = /<think>[\s\S]*?<\/think>|<thinking>[\s\S]*?<\/thinking>|\[start thinking\][\s\S]*?\[end thinking\]|\[start thinking\][\s\S]*?(?=\n## |\n# |$)|thinking process:[\s\S]*?(?=\n## |\n# |$)/gi;

const NOISE_LINE_RE = /^(loading model\b.*|build\s*:.*|model\s*:.*|ftype\s*:.*|modalities\s*:.*|available commands:.*|\/exit\b.*|\/regen\b.*|\/clear\b.*|\/read\b.*|\/glob\b.*|.*stop or exit.*|.*regenerate the last.*|.*clear the chat.*|.*add a text file.*|.*globbing pattern.*|exiting\.{0,3})$/i;

const BLOCK_ART_RE = /^[\s█░▒▓▄▀━─│|/\\-]+$/;

function stripNoiseLines(text: string) {
  return text
    .split('\n')
    .filter((line) => {
      const value = line.trim();
      if (!value) return true;
      if (BLOCK_ART_RE.test(value)) return false;
      if (NOISE_LINE_RE.test(value)) return false;
      return true;
    })
    .join('\n');
}

function afterPrompt(text: string) {
  const markers = ['转写文本：', '转写：'];
  let last = -1;
  let marker = '';
  for (const item of markers) {
    const index = text.lastIndexOf(item);
    if (index > last) {
      last = index;
      marker = item;
    }
  }
  if (last < 0) return text;
  return text.slice(last + marker.length).replace(/^\s+/, '');
}

export function cleanSummaryMarkdown(raw?: string | null) {
  let text = (raw || '').replace(/\r\n/g, '\n').trim();
  if (!text) return '';
  text = stripNoiseLines(text);
  text = text.replace(THINK_RE, '');
  text = afterPrompt(text).replace(THINK_RE, '').trim();
  const heading = text.match(/^(##\s+|#\s+)/m);
  if (heading && heading.index != null) {
    text = text.slice(heading.index).trim();
  } else if (/loading model|available commands:|\[start thinking\]|thinking process:/i.test(raw || '')) {
    return '';
  }
  return text.trim();
}
