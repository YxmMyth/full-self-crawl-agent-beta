"""Page representation — convert live page to Markdown+HTML hybrid format.

Format (D2Snap, 73% highest accuracy):
- Static text → Markdown (headings, paragraphs, lists, tables)
- Interactive elements → [N]<tag attr="val">text</tag> numbered HTML
- Images → ![alt](src) Markdown
- Containers (nav/section/main/header/footer) → preserve hierarchy, no numbering
- data-* attributes preserved (except framework-injected)
- List truncation: >5 same-type consecutive siblings → first 3 + count

See: docs/browse工具深度设计报告.md §一, AgentSession设计.md §7.6
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.browser.element_index import build_index, format_element
from src.browser.network_capture import format_network_summary
from src.utils.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.browser.context import ToolContext

logger = get_logger(__name__)

# Approximate chars per token for English/mixed content
_CHARS_PER_TOKEN = 4
_TOKEN_LIMIT = 8000
_CHAR_LIMIT = _TOKEN_LIMIT * _CHARS_PER_TOKEN  # ~32000 chars

# ── JavaScript to extract page structure ─────────────────

_EXTRACT_JS = """
() => {
    const CONTAINER_TAGS = new Set([
        'NAV', 'SECTION', 'MAIN', 'HEADER', 'FOOTER', 'ASIDE',
        'ARTICLE', 'FORM', 'FIELDSET', 'DIALOG',
    ]);
    const HEADING_TAGS = new Set(['H1', 'H2', 'H3', 'H4', 'H5', 'H6']);
    const LIST_TAGS = new Set(['UL', 'OL']);
    const SKIP_TAGS = new Set([
        'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'TEMPLATE', 'IFRAME',
        'LINK', 'META', 'HEAD',
    ]);

    // Collect data signals: script tags with embedded data
    function collectDataSignals() {
        const signals = [];

        // JSON script tags
        const jsonScripts = document.querySelectorAll(
            'script[type="application/json"], script[type="application/ld+json"]'
        );
        for (const s of jsonScripts) {
            const id = s.id || s.getAttribute('type');
            const size = (s.textContent || '').length;
            signals.push({
                type: s.getAttribute('type'),
                id: s.id || null,
                size: size,
            });
        }

        // Inline scripts that might contain data (large ones)
        const inlineScripts = document.querySelectorAll('script:not([src]):not([type])');
        let inlineCount = 0;
        let largestInline = 0;
        for (const s of inlineScripts) {
            const len = (s.textContent || '').length;
            if (len > 500) {  // Only count substantial scripts
                inlineCount++;
                if (len > largestInline) largestInline = len;
            }
        }
        if (inlineCount > 0) {
            signals.push({
                type: 'inline',
                id: null,
                size: largestInline,
                count: inlineCount,
            });
        }

        // Known framework data objects
        const globalData = [];
        if (window.__NEXT_DATA__) globalData.push('__NEXT_DATA__');
        if (window.__NUXT__) globalData.push('__NUXT__');
        if (window.__INITIAL_STATE__) globalData.push('__INITIAL_STATE__');
        if (window.__APOLLO_STATE__) globalData.push('__APOLLO_STATE__');

        return { scripts: signals, globals: globalData };
    }

    // Get scroll position info
    function getScrollInfo() {
        const scrollY = window.scrollY;
        const viewportH = window.innerHeight;
        const totalH = document.documentElement.scrollHeight;
        const pagesAbove = scrollY / viewportH;
        const pagesBelow = Math.max(0, (totalH - scrollY - viewportH) / viewportH);
        return {
            pagesAbove: Math.round(pagesAbove * 10) / 10,
            pagesBelow: Math.round(pagesBelow * 10) / 10,
            percent: totalH > 0 ? Math.round(scrollY / (totalH - viewportH) * 100) : 0,
        };
    }

    // Recursively extract page structure as a tree
    function extractNode(el, depth) {
        if (!el || SKIP_TAGS.has(el.tagName)) return null;
        if (depth > 20) return null;  // prevent infinite recursion

        const tag = el.tagName;
        const agentIdx = el.getAttribute('data-agent-idx');

        // Interactive element — handled separately via element index
        if (agentIdx) {
            return { type: 'indexed', idx: parseInt(agentIdx) };
        }

        // Image
        if (tag === 'IMG') {
            const alt = el.alt || '';
            const src = el.src || '';
            if (!src) return null;
            return { type: 'image', alt, src };
        }

        // Heading
        if (HEADING_TAGS.has(tag)) {
            const text = (el.innerText || '').trim();
            if (!text) return null;
            const level = parseInt(tag[1]);
            return { type: 'heading', level, text };
        }

        // Table
        if (tag === 'TABLE') {
            return extractTable(el);
        }

        // List
        if (LIST_TAGS.has(tag)) {
            return extractList(el, depth);
        }

        // Container — preserve hierarchy
        if (CONTAINER_TAGS.has(tag) || tag === 'DIV') {
            const children = extractChildren(el, depth + 1);
            if (children.length === 0) return null;
            // Only wrap in container tag for semantic elements, not plain divs
            if (CONTAINER_TAGS.has(tag)) {
                const cls = el.className ?
                    el.className.toString().split(' ').slice(0, 2).join(' ') : '';
                return {
                    type: 'container',
                    tag: tag.toLowerCase(),
                    class: cls,
                    children
                };
            }
            // Plain div with meaningful class — might be a card/item
            if (children.length <= 10) {
                return { type: 'group', children };
            }
            return { type: 'group', children };
        }

        // Paragraph / text content
        if (tag === 'P' || tag === 'SPAN' || tag === 'LABEL' ||
            tag === 'FIGCAPTION' || tag === 'BLOCKQUOTE' || tag === 'PRE' ||
            tag === 'CODE' || tag === 'STRONG' || tag === 'EM' || tag === 'LI') {
            // Might contain indexed children
            const children = extractChildren(el, depth + 1);
            const directText = getDirectText(el);
            if (!directText && children.length === 0) return null;
            return { type: 'text', text: directText, children };
        }

        // Generic fallback — try to extract children
        const children = extractChildren(el, depth + 1);
        if (children.length > 0) return { type: 'group', children };

        // Leaf text node
        const text = (el.innerText || '').trim();
        if (text && text.length > 2) {
            return { type: 'text', text: text.slice(0, 200) };
        }

        return null;
    }

    function extractChildren(el, depth) {
        const children = [];
        for (const child of el.children) {
            const node = extractNode(child, depth);
            if (node) children.push(node);
        }
        return children;
    }

    function getDirectText(el) {
        let text = '';
        for (const node of el.childNodes) {
            if (node.nodeType === 3) {  // TEXT_NODE
                text += node.textContent;
            }
        }
        return text.trim().slice(0, 300);
    }

    function extractTable(table) {
        const rows = [];
        for (const tr of table.querySelectorAll('tr')) {
            const cells = [];
            for (const td of tr.querySelectorAll('th, td')) {
                cells.push((td.innerText || '').trim().slice(0, 100));
            }
            if (cells.length > 0) rows.push(cells);
        }
        if (rows.length === 0) return null;
        // Truncate large tables
        const maxRows = 10;
        const truncated = rows.length > maxRows;
        return {
            type: 'table',
            rows: rows.slice(0, maxRows),
            totalRows: rows.length,
            truncated,
        };
    }

    function extractList(list, depth) {
        const items = [];
        const ordered = list.tagName === 'OL';
        for (const li of list.children) {
            if (li.tagName !== 'LI') continue;
            const node = extractNode(li, depth + 1);
            items.push(node || { type: 'text', text: (li.innerText || '').trim().slice(0, 200) });
        }
        return { type: 'list', ordered, items, total: items.length };
    }

    // Main extraction
    const tree = extractChildren(document.body || document.documentElement, 0);
    const dataSignals = collectDataSignals();
    const scrollInfo = getScrollInfo();

    return {
        title: document.title || '',
        url: location.href,
        tree,
        dataSignals,
        scrollInfo,
    };
}
"""


# ── Python-side rendering ────────────────────────────────


async def build_page_repr(
    page: Page,
    ctx: ToolContext,
) -> str:
    """Build complete page representation.

    1. Build element index (assigns data-agent-idx)
    2. Extract page structure tree
    3. Render to Markdown+HTML hybrid
    4. Append Data Signals and Network sections
    5. Enforce token limit

    Returns:
        Complete page representation string.
    """
    # Step 1: Build element index (modifies DOM with data-agent-idx)
    elements = await build_index(page, ctx)
    element_map = {el["idx"]: el for el in elements}

    # Step 2: Extract page structure
    try:
        extracted = await page.evaluate(_EXTRACT_JS)
    except Exception as e:
        logger.warning(f"Page extraction failed: {e}")
        # Fallback: just return basic info
        return f"=== Page: Error ===\nURL: {page.url}\nExtraction failed: {e}"

    # Step 3: Render
    title = extracted.get("title", "")
    url = extracted.get("url", page.url)
    tree = extracted.get("tree", [])
    data_signals = extracted.get("dataSignals", {})
    scroll_info = extracted.get("scrollInfo", {})

    lines: list[str] = []
    lines.append(f"=== Page: {title} ===")
    lines.append(f"URL: {url}")
    lines.append("")
    lines.append("--- Content ---")

    # Render tree nodes
    _render_nodes(tree, element_map, ctx, lines, depth=0)

    # Scroll position
    above = scroll_info.get("pagesAbove", 0)
    below = scroll_info.get("pagesBelow", 0)
    if above > 0.1 or below > 0.1:
        lines.append("")
        lines.append("--- Scroll Position ---")
        lines.append(f"{above:.1f} pages above | {below:.1f} pages below")

    # Data Signals section
    signals_text = _format_data_signals(data_signals)
    if signals_text:
        lines.append("")
        lines.append(signals_text)

    # Network section
    network_text = format_network_summary(ctx)
    if network_text:
        lines.append("")
        lines.append(network_text)

    result = "\n".join(lines)

    # Enforce token limit
    if len(result) > _CHAR_LIMIT:
        result = result[:_CHAR_LIMIT]
        result += f"\n[truncated at ~{_TOKEN_LIMIT} tokens, scroll + browse() for more]"

    return result


def _render_nodes(
    nodes: list[dict],
    element_map: dict[int, dict],
    ctx: ToolContext,
    lines: list[str],
    depth: int,
) -> None:
    """Recursively render tree nodes to lines."""
    # List truncation: detect consecutive same-type siblings
    consecutive_groups = _group_consecutive(nodes)

    for group in consecutive_groups:
        if group["truncated"]:
            # Render first 3, then count
            for node in group["items"][:3]:
                _render_node(node, element_map, ctx, lines, depth)
            remaining = group["total"] - 3
            indent = "  " * depth
            lines.append(f"{indent}... ({remaining} more, {group['total']} total)")
        else:
            for node in group["items"]:
                _render_node(node, element_map, ctx, lines, depth)


def _render_node(
    node: dict,
    element_map: dict[int, dict],
    ctx: ToolContext,
    lines: list[str],
    depth: int,
) -> None:
    """Render a single tree node."""
    if not node:
        return

    indent = "  " * depth
    node_type = node.get("type")

    if node_type == "indexed":
        # Interactive element — format from element_map
        idx = node["idx"]
        el = element_map.get(idx)
        if el:
            is_new = str(idx) not in ctx.previous_element_ids and bool(ctx.previous_element_ids)
            lines.append(f"{indent}{format_element(el, is_new=is_new)}")

    elif node_type == "heading":
        level = node.get("level", 1)
        text = node.get("text", "")
        prefix = "#" * level
        lines.append(f"{indent}{prefix} {text}")
        lines.append("")

    elif node_type == "text":
        text = node.get("text", "")
        children = node.get("children", [])
        if text:
            lines.append(f"{indent}{text}")
        if children:
            _render_nodes(children, element_map, ctx, lines, depth)

    elif node_type == "image":
        alt = node.get("alt", "")
        src = node.get("src", "")
        # Truncate long src URLs
        if len(src) > 100:
            src = src[:97] + "..."
        lines.append(f"{indent}![{alt}]({src})")

    elif node_type == "container":
        tag = node.get("tag", "div")
        cls = node.get("class", "")
        cls_attr = f' class="{cls}"' if cls else ""
        lines.append(f"{indent}<{tag}{cls_attr}>")
        children = node.get("children", [])
        _render_nodes(children, element_map, ctx, lines, depth + 1)
        lines.append(f"{indent}</{tag}>")

    elif node_type == "group":
        children = node.get("children", [])
        _render_nodes(children, element_map, ctx, lines, depth)

    elif node_type == "table":
        _render_table(node, lines, indent)

    elif node_type == "list":
        _render_list(node, element_map, ctx, lines, depth)


def _render_table(node: dict, lines: list[str], indent: str) -> None:
    """Render a table as Markdown."""
    rows = node.get("rows", [])
    if not rows:
        return

    # First row as header
    header = rows[0]
    lines.append(f"{indent}| {' | '.join(header)} |")
    lines.append(f"{indent}| {' | '.join('---' for _ in header)} |")

    for row in rows[1:]:
        # Pad row to match header length
        padded = row + [""] * (len(header) - len(row))
        lines.append(f"{indent}| {' | '.join(padded[:len(header)])} |")

    if node.get("truncated"):
        lines.append(f"{indent}... ({node['totalRows'] - len(rows)} more rows, {node['totalRows']} total)")
    lines.append("")


def _render_list(
    node: dict,
    element_map: dict[int, dict],
    ctx: ToolContext,
    lines: list[str],
    depth: int,
) -> None:
    """Render a list with truncation."""
    items = node.get("items", [])
    ordered = node.get("ordered", False)
    total = node.get("total", len(items))
    indent = "  " * depth

    # Apply truncation: >5 items → show first 3
    show_items = items
    truncated = False
    if total > 5:
        show_items = items[:3]
        truncated = True

    for i, item in enumerate(show_items):
        marker = f"{i + 1}." if ordered else "-"
        if item and item.get("type") == "text":
            text = item.get("text", "")
            lines.append(f"{indent}{marker} {text}")
            # Render any indexed children within list items
            children = item.get("children", [])
            if children:
                _render_nodes(children, element_map, ctx, lines, depth + 1)
        else:
            lines.append(f"{indent}{marker} (complex item)")
            if item:
                _render_node(item, element_map, ctx, lines, depth + 1)

    if truncated:
        lines.append(f"{indent}... ({total - 3} more, {total} total)")


def _group_consecutive(nodes: list[dict]) -> list[dict]:
    """Group consecutive same-type nodes for truncation.

    If >5 consecutive nodes share the same structure (same type + similar shape),
    mark the group for truncation.
    """
    if not nodes:
        return []

    groups: list[dict] = []
    current_group: list[dict] = [nodes[0]]
    current_sig = _node_signature(nodes[0])

    for node in nodes[1:]:
        sig = _node_signature(node)
        if sig == current_sig and sig is not None:
            current_group.append(node)
        else:
            groups.append({
                "items": current_group,
                "total": len(current_group),
                "truncated": len(current_group) > 5,
            })
            current_group = [node]
            current_sig = sig

    groups.append({
        "items": current_group,
        "total": len(current_group),
        "truncated": len(current_group) > 5,
    })

    return groups


def _node_signature(node: dict) -> str | None:
    """Compute a rough signature for a node to detect repeated patterns."""
    if not node:
        return None
    t = node.get("type")
    if t == "container":
        return f"container:{node.get('tag')}"
    if t == "group":
        # Groups with similar child counts are likely repeated cards
        children = node.get("children", [])
        child_types = tuple(c.get("type") for c in children[:3] if c)
        return f"group:{len(children)}:{child_types}"
    return None  # Don't group dissimilar types


# ── Data signals formatting ──────────────────────────────


def _format_data_signals(signals: dict) -> str:
    """Format data signals section."""
    scripts = signals.get("scripts", [])
    globals_list = signals.get("globals", [])

    if not scripts and not globals_list:
        return ""

    lines = ["--- Data Signals ---"]

    if globals_list:
        lines.append(f"Global data objects: {', '.join(globals_list)}")

    if scripts:
        lines.append(f"Script data ({len(scripts)} found):")
        for s in scripts:
            if s["type"] == "inline":
                count = s.get("count", 0)
                size = _human_size(s["size"])
                lines.append(f"  inline <script>: {count} blocks, largest {size}")
            else:
                id_part = f' id="{s["id"]}"' if s.get("id") else ""
                size = _human_size(s["size"])
                lines.append(f'  <script{id_part} type="{s["type"]}">: {size}')

    return "\n".join(lines)


def _human_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"
