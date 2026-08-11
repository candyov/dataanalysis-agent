/** Markdown 渲染工具 */

import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({
  breaks: true,
  gfm: true,
})

export function markdownToHtml(text: string): string {
  if (!text) return ''
  // 优先级标记美化: 旧报告可能含 "[高]/[中]/[低]" 文本, 统一转 emoji (兼容旧会话数据)
  const upgraded = text
    .replace(/\[高\]/g, '🔴')
    .replace(/\[中\]/g, '🟡')
    .replace(/\[低\]/g, '🟢')
  const raw = marked.parse(upgraded) as string
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'strong', 'em', 'u', 's',
      'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'a', 'table', 'thead', 'tbody', 'tr',
      'th', 'td', 'hr', 'img', 'span', 'div', 'sup', 'sub'],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'style',
      'colspan', 'rowspan', 'width', 'height'],
  }) as string
}

/** 对裸 HTML 内容做 XSS 防护 (非 markdown 场景) */
export function sanitizeHtml(html: string): string {
  if (!html) return ''
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'strong', 'em', 'u', 's',
      'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'a', 'table', 'thead', 'tbody', 'tr',
      'th', 'td', 'hr', 'img', 'span', 'div', 'sup', 'sub'],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'style',
      'colspan', 'rowspan', 'width', 'height'],
  }) as string
}
