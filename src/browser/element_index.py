"""Element indexing system — number interactive elements for agent interaction.

Injects JavaScript to traverse the DOM, identify interactive elements,
assign sequential numbers, and build a selector map (N → CSS selector).

Each element gets a `data-agent-idx` attribute for reliable re-selection.
Index is rebuilt after every browse()/interaction, never persistent across pages.

See: docs/browse工具深度设计报告.md §二
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.utils.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.browser.context import ToolContext

logger = get_logger(__name__)

# ── JavaScript for DOM traversal and element indexing ────

_INDEX_JS = """
() => {
    // Clean up previous indexing
    document.querySelectorAll('[data-agent-idx]').forEach(el => {
        el.removeAttribute('data-agent-idx');
    });

    const INTERACTIVE_TAGS = new Set([
        'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT', 'DETAILS', 'SUMMARY'
    ]);

    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'tab', 'checkbox', 'radio', 'switch',
        'menuitem', 'menuitemcheckbox', 'menuitemradio',
        'option', 'combobox', 'textbox', 'searchbox',
        'slider', 'spinbutton', 'listbox', 'treeitem'
    ]);

    // Attributes worth keeping in element representation
    const KEEP_ATTRS = new Set([
        'type', 'placeholder', 'href', 'name', 'value',
        'aria-label', 'role', 'title', 'alt', 'for',
        'checked', 'disabled', 'readonly', 'required',
        'min', 'max', 'step', 'pattern',
    ]);

    // Framework-injected data-* attrs to filter out
    const FRAMEWORK_DATA_PREFIXES = [
        'data-reactid', 'data-react-', 'data-v-', 'data-testid',
        'data-test-', 'data-cy', 'data-qa', 'data-automation',
        'data-gtm', 'data-analytics', 'data-track',
    ];

    function isVisible(el) {
        if (!el.offsetParent && el.tagName !== 'BODY' && el.tagName !== 'HTML') {
            // Check if it's position:fixed/sticky (still visible without offsetParent)
            const style = getComputedStyle(el);
            if (style.position !== 'fixed' && style.position !== 'sticky') {
                return false;
            }
        }
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
        return true;
    }

    function isInteractive(el) {
        if (INTERACTIVE_TAGS.has(el.tagName)) return true;
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role.toLowerCase())) return true;
        if (el.hasAttribute('onclick')) return true;
        if (el.hasAttribute('contenteditable') &&
            el.getAttribute('contenteditable') !== 'false') return true;
        const tabindex = el.getAttribute('tabindex');
        if (tabindex !== null && parseInt(tabindex) >= 0 &&
            !INTERACTIVE_TAGS.has(el.tagName)) return true;
        return false;
    }

    function isFrameworkData(attrName) {
        const lower = attrName.toLowerCase();
        return FRAMEWORK_DATA_PREFIXES.some(prefix => lower.startsWith(prefix));
    }

    function getAttrs(el) {
        const attrs = {};
        for (const attr of el.attributes) {
            const name = attr.name.toLowerCase();
            // Keep standard interactive attributes
            if (KEEP_ATTRS.has(name)) {
                let val = attr.value;
                // Truncate long hrefs to path
                if (name === 'href' && val.length > 80) {
                    try {
                        const u = new URL(val, location.origin);
                        val = u.pathname + (u.search || '');
                    } catch(e) { val = val.slice(0, 80); }
                }
                attrs[name] = val;
                continue;
            }
            // Keep business-meaningful data-* (filter framework-injected)
            if (name.startsWith('data-') && !isFrameworkData(name)) {
                attrs[name] = attr.value.slice(0, 100);
            }
        }
        return attrs;
    }

    function getVisibleText(el) {
        // For inputs, use placeholder or value
        if (el.tagName === 'INPUT') {
            return el.placeholder || el.value || el.type || '';
        }
        if (el.tagName === 'TEXTAREA') {
            return el.placeholder || el.value?.slice(0, 40) || '';
        }
        // For select, show current selection + option count
        if (el.tagName === 'SELECT') {
            const selected = el.options[el.selectedIndex]?.text || '';
            return `[${selected}] (${el.options.length} options)`;
        }
        // Direct text content, not children's text
        let text = '';
        for (const node of el.childNodes) {
            if (node.nodeType === 3) { // TEXT_NODE
                text += node.textContent;
            }
        }
        text = text.trim();
        if (!text) {
            // Fallback: aria-label, title, or innerText
            text = el.getAttribute('aria-label') ||
                   el.getAttribute('title') ||
                   el.innerText?.trim() || '';
        }
        // Truncate to 80 chars
        if (text.length > 80) text = text.slice(0, 77) + '...';
        return text;
    }

    // Traverse DOM in order, collect interactive elements
    const elements = [];
    let idx = 1;

    const walker = document.createTreeWalker(
        document.body || document.documentElement,
        NodeFilter.SHOW_ELEMENT,
        null
    );

    let node = walker.currentNode;
    while (node) {
        if (isInteractive(node) && isVisible(node)) {
            node.setAttribute('data-agent-idx', String(idx));

            elements.push({
                idx: idx,
                tag: node.tagName.toLowerCase(),
                attrs: getAttrs(node),
                text: getVisibleText(node),
                // Generate a unique selector
                selector: `[data-agent-idx="${idx}"]`,
            });
            idx++;
        }
        node = walker.nextNode();
    }

    return { elements, count: elements.length };
}
"""


async def build_index(page: Page, ctx: ToolContext) -> list[dict]:
    """Build element index on the current page.

    Injects JS to traverse DOM, identify interactive elements,
    assign data-agent-idx attributes, and return element info.

    Updates ctx.selector_map with N → CSS selector mappings.

    Returns:
        List of element dicts: {idx, tag, attrs, text, selector}
    """
    ctx.clear_selector_map()

    try:
        result = await page.evaluate(_INDEX_JS)
    except Exception as e:
        logger.warning(f"Element indexing failed: {e}")
        return []

    elements = result.get("elements", [])

    # Build selector map
    for el in elements:
        ctx.selector_map[el["idx"]] = el["selector"]

    logger.debug(
        f"Indexed {len(elements)} interactive elements",
        extra={"url": page.url},
    )
    return elements


def format_element(el: dict, is_new: bool = False) -> str:
    """Format a single indexed element as [N]<tag attrs>text</tag>.

    Args:
        el: Element dict from build_index.
        is_new: If True, prefix with * to mark as newly appeared.
    """
    prefix = "*" if is_new else ""
    tag = el["tag"]
    text = el["text"]
    idx = el["idx"]

    # Build attribute string
    attrs = el.get("attrs", {})
    attr_parts = []
    for k, v in attrs.items():
        if v:
            attr_parts.append(f'{k}="{v}"')
        else:
            attr_parts.append(k)
    attr_str = " " + " ".join(attr_parts) if attr_parts else ""

    return f"{prefix}[{idx}]<{tag}{attr_str}>{text}</{tag}>"
