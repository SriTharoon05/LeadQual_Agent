import { memo, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';

type Alignment = 'left' | 'center' | 'right';
type Block = { kind: 'markdown'; content: string } | {
  kind: 'table'; headers: string[]; rows: string[][]; alignment: Alignment[];
};

function safeLink(value: string | undefined): string | null {
  if (!value || !/^https?:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    return url.hostname && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

const components: Components = {
  a: ({ href, children }) => {
    const url = safeLink(href);
    if (!url) return <span>{children}</span>;
    const text = typeof children === 'string' ? children : Array.isArray(children) && children.every((part) => typeof part === 'string') ? children.join('') : '';
    const label = /^https?:\/\//i.test(text) && text.length > 60 ? new URL(url).hostname.replace(/^www\./, '') : children;
    return <a href={url} target="_blank" rel="noopener noreferrer" title={url} className="font-medium text-sky-700 underline decoration-sky-200 underline-offset-2">{label}</a>;
  },
  img: ({ alt }) => <span>{alt || 'Image'}</span>,
  strong: ({ children }) => <strong className="font-semibold text-slate-950">{children}</strong>,
  h1: ({ children }) => <h3 className="mb-2 mt-3 text-base font-bold text-slate-900 first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-1.5 mt-3 text-sm font-bold text-slate-900 first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1 mt-2.5 text-sm font-semibold text-slate-900 first:mt-0">{children}</h4>,
  p: ({ children }) => <p className="my-1.5 whitespace-pre-line leading-relaxed first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5 marker:text-brand-500">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5 marker:font-medium marker:text-slate-500">{children}</ol>,
  li: ({ children }) => <li className="pl-0.5 leading-relaxed">{children}</li>,
  blockquote: ({ children }) => <blockquote className="my-2 border-l-2 border-brand-200 bg-brand-50/50 py-1 pl-3 pr-2 text-slate-600">{children}</blockquote>,
  pre: ({ children }) => <pre tabIndex={0} className="my-2 max-w-full overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100 [&>code]:bg-transparent [&>code]:p-0 [&>code]:text-inherit">{children}</pre>,
  code: ({ children, className }) => <code className={className || 'rounded bg-slate-100 px-1 py-0.5 text-[0.85em] text-slate-800'}>{children}</code>,
  hr: () => <hr className="my-3 border-slate-200" />,
};

const cellComponents: Components = {
  ...components,
  p: ({ children }) => <span>{children}</span>,
};

/** Split only structural pipes, preserving escaped pipes and inline code. */
function cells(line: string): string[] {
  const text = line.trim();
  const result: string[] = [];
  let current = '';
  let codeTicks = 0;
  for (let index = 0; index < text.length; index++) {
    const character = text[index];
    if (character === '\\' && index + 1 < text.length) {
      current += character + text[++index];
    } else if (character === '`') {
      let end = index;
      while (text[end] === '`') end++;
      const length = end - index;
      if (codeTicks === 0) codeTicks = length;
      else if (codeTicks === length) codeTicks = 0;
      current += text.slice(index, end);
      index = end - 1;
    } else if (character === '|' && codeTicks === 0) {
      result.push(current.trim()); current = '';
    } else current += character;
  }
  result.push(current.trim());
  if (text.startsWith('|') && result[0] === '') result.shift();
  if (text.endsWith('|') && result[result.length - 1] === '') result.pop();
  return result;
}

/** GFM-style pipe tables without adding an unverified package/lockfile change.
 * Code fences remain ordinary Markdown; table cells use the same safe renderer.
 */
function blocks(content: string): Block[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const output: Block[] = [];
  let markdown: string[] = [];
  let fence = '';
  let fenceLength = 0;
  const flush = () => {
    if (markdown.some((line) => line.trim())) output.push({ kind: 'markdown', content: markdown.join('\n') });
    markdown = [];
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (marker) {
      if (!fence) { fence = marker[1][0]; fenceLength = marker[1].length; }
      else if (marker[1][0] === fence && marker[1].length >= fenceLength && !marker[2].trim()) fence = '';
      markdown.push(line);
      continue;
    }
    if (fence || /^(?: {4}|\t)/.test(line) || !line.includes('|') || index + 1 >= lines.length) { markdown.push(line); continue; }
    const headers = cells(line);
    const separators = cells(lines[index + 1]);
    if (!headers.length || headers.length !== separators.length || !lines[index + 1].includes('|')
        || !separators.every((cell) => /^:?-{3,}:?$/.test(cell))) { markdown.push(line); continue; }
    flush();
    const alignment: Alignment[] = separators.map((value) => value.startsWith(':') && value.endsWith(':') ? 'center' : value.endsWith(':') ? 'right' : 'left');
    const rows: string[][] = [];
    index++;
    while (index + 1 < lines.length && lines[index + 1].trim() && lines[index + 1].includes('|')) {
      // A fence or a new table starts a new block instead of becoming a data row.
      if (/^ {0,3}(`{3,}|~{3,})/.test(lines[index + 1])) break;
      const values = cells(lines[index + 1]);
      if (index + 2 < lines.length && lines[index + 2].includes('|')) {
        const nextSeparators = cells(lines[index + 2]);
        if (nextSeparators.length === values.length && nextSeparators.every((value) => /^:?-{3,}:?$/.test(value))) break;
      }
      index++;
      while (values.length < headers.length) values.push('');
      if (values.length > headers.length) values.splice(headers.length - 1, values.length, values.slice(headers.length - 1).join(' | '));
      rows.push(values);
    }
    output.push({ kind: 'table', headers, rows, alignment });
  }
  flush();
  return output;
}

export const ChatMarkdown = memo(function ChatMarkdown({ content }: { content: string }) {
  const parts = useMemo(() => blocks(content), [content]);
  return <div className="min-w-0 space-y-2 [overflow-wrap:anywhere]">
    {parts.map((part, index) => part.kind === 'markdown' ? (
      <ReactMarkdown key={index} skipHtml components={components}>{part.content}</ReactMarkdown>
    ) : (
      <div key={index} role="region" aria-label="Lead data table" tabIndex={0} className="my-2 max-w-full overflow-x-auto rounded-lg border border-slate-200 bg-white focus:outline-none focus:ring-2 focus:ring-brand-200">
        <table className="w-full border-collapse text-xs leading-relaxed">
          <thead className="bg-slate-100/80">
            <tr>{part.headers.map((header, column) => <th key={column} scope="col" style={{ textAlign: part.alignment[column] }} className="min-w-[7rem] border-b border-slate-200 px-3 py-2 font-semibold text-slate-900">
              <ReactMarkdown skipHtml components={cellComponents}>{header || '—'}</ReactMarkdown>
            </th>)}</tr>
          </thead>
          <tbody>{part.rows.map((row, rowIndex) => <tr key={rowIndex} className="even:bg-slate-50/70">
            {row.map((value, column) => <td key={column} style={{ textAlign: part.alignment[column] }} className="max-w-[24rem] border-b border-slate-100 px-3 py-2 align-top last:border-r-0">
              <ReactMarkdown skipHtml components={cellComponents}>{value.replace(/<br\s*\/?>/gi, '  \n') || '—'}</ReactMarkdown>
            </td>)}
          </tr>)}</tbody>
        </table>
      </div>
    ))}
  </div>;
});
