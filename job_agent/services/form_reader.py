"""
Form Reader (Phase 5).

Extracts the fillable fields from a job application form in the browser.

Everything runs in one page.evaluate() call rather than per-field Playwright
queries: a long application form has 30+ inputs, and a round trip each would be
both slow and prone to the auto-wait stalls that per-selector probing caused in
the Phase 3 connector.

For each field it captures the text a human would read — the associated
<label>, aria-label, placeholder, or nearby text — because that, not the HTML
`name` attribute, is what determines whether a question is sensitive. A field
named `q_12345` asking "Are you a protected veteran?" must be recognized from
its label.
"""

import logging
import unicodedata
from typing import Any, List

from job_agent.services.field_classifier import FormField

logger = logging.getLogger(__name__)

# Characters that mojibake always produces, used to decide whether repair is
# worth attempting at all.
_MOJIBAKE_MARKERS = ("â€", "Ã", "Â", "�")

# Typographic quotes folded to ASCII. NFKC leaves these alone (they are not
# compatibility decompositions), but the sensitive-question patterns are
# written with straight quotes, so "What's your current salary?" typeset with a
# curly apostrophe would not match.
_QUOTE_FOLDING = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
})

# Extraction runs entirely in the page. Returns one record per fillable field.
_EXTRACT_FIELDS_JS = """
() => {
    const cssEscape = (value) =>
        window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/["\\\\]/g, '\\\\$&');

    // A stable selector for filling the field later
    const selectorFor = (el) => {
        if (el.id) return '#' + cssEscape(el.id);
        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
        const parent = el.parentElement;
        if (!parent) return el.tagName.toLowerCase();
        const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        const index = siblings.indexOf(el) + 1;
        const parentSel = parent.id ? '#' + cssEscape(parent.id) : parent.tagName.toLowerCase();
        return parentSel + ' > ' + el.tagName.toLowerCase() + ':nth-of-type(' + index + ')';
    };

    // The question a group of choices belongs to, which is never the text
    // beside any one choice. Indeed's SmartApply wraps each employer question
    // in <fieldset role="radiogroup"> with the question in its <legend>, and
    // gives every radio its own <label> reading "Yes" or "No" — so reading the
    // input's own label reported an employer's SMS-consent question as a
    // question called "No". A wrong question text is worse than none: it is
    // what the answer is filed under, so nothing the user answers would ever
    // carry to the next employer asking the same thing.
    const groupLabelFor = (el) => {
        const fieldset = el.closest('fieldset');
        if (fieldset) {
            const legend = fieldset.querySelector(':scope > legend');
            if (legend && legend.innerText.trim()) return legend.innerText.trim();
        }
        const group = el.closest('[role="radiogroup"], [role="group"]');
        if (group) {
            const id = group.getAttribute('aria-labelledby');
            if (id) {
                const text = id.split(/\s+/)
                    .map(x => document.getElementById(x))
                    .filter(Boolean)
                    .map(n => n.innerText.trim())
                    .filter(Boolean)
                    .join(' ');
                if (text) return text;
            }
            const aria = group.getAttribute('aria-label');
            if (aria && aria.trim()) return aria.trim();
        }
        return '';
    };

    // The text a person reads next to the input
    const labelFor = (el) => {
        // For one option of a group, the group's own question comes first.
        const type = (el.type || '').toLowerCase();
        if (type === 'radio' || type === 'checkbox') {
            const group = groupLabelFor(el);
            if (group) return group;
        }
        if (el.id) {
            const explicit = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
            if (explicit && explicit.innerText.trim()) return explicit.innerText.trim();
        }
        const wrapping = el.closest('label');
        if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim();

        // A fieldset legend covers radio/checkbox groups
        const fieldset = el.closest('fieldset');
        if (fieldset) {
            const legend = fieldset.querySelector('legend');
            if (legend && legend.innerText.trim()) return legend.innerText.trim();
        }

        // Fall back to the nearest preceding text node in the container
        const container = el.closest('div, p, li, td, section');
        if (container) {
            const text = Array.from(container.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim())
                .filter(Boolean)
                .join(' ');
            if (text) return text;
            const heading = container.querySelector('label, legend, h1, h2, h3, h4, strong, b');
            if (heading && heading.innerText.trim()) return heading.innerText.trim();
        }
        return '';
    };

    // Modern forms replace <select> with a text input driving a custom list.
    // The choices are still in the page — in a hidden native select the form
    // actually submits, or in a listbox the input points at — and a question
    // whose answer must come from a fixed list is unanswerable without them.
    const optionsForCombobox = (el) => {
        const collect = (node) => node
            ? Array.from(node.querySelectorAll('option, [role="option"]'))
                .map(o => (o.innerText || o.textContent || '').trim())
                .filter(Boolean)
            : [];

        // A hidden native select carrying the same name or id
        const twins = [];
        if (el.name) twins.push('select[name="' + el.name + '"]');
        if (el.id) twins.push('select#' + cssEscape(el.id));
        for (const selector of twins) {
            const found = document.querySelector(selector);
            if (found) {
                const values = Array.from(found.options)
                    .map(o => o.text.trim()).filter(Boolean);
                if (values.length) return values;
            }
        }

        // A listbox the input names through ARIA
        for (const attribute of ['aria-controls', 'aria-owns']) {
            const id = el.getAttribute(attribute);
            if (!id) continue;
            const values = collect(document.getElementById(id));
            if (values.length) return values;
        }

        // Deliberately no "nearest select in the same container" fallback:
        // forms group several questions in one wrapper, and proximity then
        // hands one question's choices to another. A wrong list of options is
        // worse than none — it makes an unrelated answer look valid.
        return [];
    };

    const skipTypes = new Set(['submit', 'button', 'reset', 'image', 'hidden']);
    const results = [];
    const seenRadioGroups = new Set();

    document.querySelectorAll('input, select, textarea').forEach((el) => {
        const tag = el.tagName.toLowerCase();
        const type = (el.type || tag).toLowerCase();

        if (skipTypes.has(type)) return;
        if (el.disabled || el.readOnly) return;

        // Skip invisible fields — a form may hold several hidden steps
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        if (el.offsetParent === null && style.position !== 'fixed') return;

        // A custom select keeps an unnamed proxy input inside its shell so the
        // browser still enforces "required". It is not a question: it has no
        // id, no name and no label of any kind, and the question it belongs to
        // is captured separately from its own label. Reported as a field it
        // reaches the user as "div > input:nth-of-type(1)", which nobody can
        // answer, and it cannot be filled reliably either.
        const hasIdentity = el.id || el.name;
        const hasWording = labelFor(el) || el.getAttribute('aria-label')
            || el.placeholder;

        if (!hasIdentity && !hasWording) return;

        // Radio groups are one question, not one per option
        if (type === 'radio') {
            if (!el.name || seenRadioGroups.has(el.name)) return;
            seenRadioGroups.add(el.name);
        }

        let options = [];
        if (tag === 'select') {
            options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
        } else if (optionsForCombobox(el).length) {
            // A custom dropdown: the visible control is a text input, so
            // without this the user is asked to type an answer the form will
            // only accept from its own list.
            options = optionsForCombobox(el);
        } else if (type === 'radio' && el.name) {
            options = Array.from(document.querySelectorAll(
                'input[type="radio"][name="' + el.name + '"]'
            )).map(r => {
                const lbl = r.id ? document.querySelector('label[for="' + cssEscape(r.id) + '"]') : null;
                return (lbl ? lbl.innerText : r.value || '').trim();
            }).filter(Boolean);
        }

        // What the form already holds for this field. A step that arrives
        // with an answer in it is not an unanswered question: Indeed's resume
        // step opens with the user's resume already chosen, and reporting it
        // as needing them put "Add a resume" at the top of the tray on an
        // application that had one.
        let answered = false;
        let current = '';
        if (type === 'radio' && el.name) {
            const chosen = [...document.querySelectorAll(
                'input[type="radio"][name="' + el.name + '"]'
            )].find(r => r.checked);
            if (chosen) {
                answered = true;
                const lbl = chosen.id
                    ? document.querySelector('label[for="' + cssEscape(chosen.id) + '"]')
                    : null;
                current = (lbl ? lbl.innerText : chosen.value || '').trim();
            }
        } else if (type === 'checkbox') {
            answered = el.checked;
            current = el.checked ? 'checked' : '';
        } else {
            current = (el.value || '').trim();
            answered = !!current;
        }

        results.push({
            answered: answered,
            current: current,
            selector: selectorFor(el),
            tag: tag,
            field_type: type,
            name: el.name || '',
            field_id: el.id || '',
            label: labelFor(el),
            placeholder: el.placeholder || '',
            aria_label: el.getAttribute('aria-label') || '',
            // A form need not use the `required` attribute to mean it.
            // Indeed marks its required questions with a trailing asterisk in
            // the legend and nowhere else, so a walk that trusted the
            // attribute pressed Continue past an unanswered required question
            // and only learned better when the form refused to advance.
            required: !!el.required
                || el.getAttribute('aria-required') === 'true'
                || /[*\u2217]\s*$/.test((labelFor(el) || '').trim()),
            options: options,
            // Whether this control is a dropdown wearing a text input's
            // clothes. Only these are worth opening to read their choices;
            // clicking an ordinary text box or textarea opens nothing and
            // risks crediting it with a list that belongs to another field.
            looks_like_dropdown: tag !== 'textarea' && (
                el.getAttribute('role') === 'combobox'
                || el.hasAttribute('aria-haspopup')
                || el.hasAttribute('aria-expanded')
                || el.readOnly
                || /select|combobox|dropdown/i.test(
                    (el.closest('div, span')?.className || '') + ' ' + (el.className || '')
                )
            )
        });
    });

    return results;
}
"""


# Opening dropdowns costs a click and a wait each; a form with dozens of them
# would otherwise stall the run for no proportionate gain.
MAX_DROPDOWNS_PROBED = 25
DROPDOWN_OPEN_TIMEOUT_MS = 1500

# A ceiling only against a pathological page, not against ordinary questions.
# "Which country do you live in?" has ~200 answers and a phone-code list has
# about the same; a limit anywhere near those truncates real choices, and the
# user then cannot pick the answer their form requires.
MAX_CHOICES_PER_FIELD = 400

# Choices currently on screen. Options inside a native <select> are excluded:
# they belong to that select, which is read directly, and counting them here
# would attribute one field's list to another.
_VISIBLE_CHOICES_JS = """() => Array.from(
    document.querySelectorAll('[role="option"], [class*="option"]')
)
    .filter(el => !el.closest('select'))
    .filter(el => el.offsetParent !== null)
    .map(el => (el.innerText || '').trim())
    .filter(Boolean)
    .slice(0, 400)"""


class FormReader:
    """Reads application form fields from a live page."""

    @staticmethod
    async def read_fields(page: Any) -> List[FormField]:
        """
        Extract every fillable field from the current page.

        Args:
            page: Playwright page positioned on the application form

        Returns:
            FormField records, in document order
        """
        raw = await page.evaluate(_EXTRACT_FIELDS_JS)

        fields = [
            FormField(
                selector=item["selector"],
                tag=item["tag"],
                field_type=item["field_type"],
                name=item.get("name", ""),
                field_id=item.get("field_id", ""),
                label=FormReader._tidy(item.get("label", "")),
                placeholder=item.get("placeholder", ""),
                aria_label=item.get("aria_label", ""),
                required=bool(item.get("required")),
                options=item.get("options") or [],
                answered=bool(item.get("answered")),
                current=FormReader._tidy(item.get("current", "")),
            )
            for item in raw
        ]

        openable = {
            item["selector"] for item in raw if item.get("looks_like_dropdown")
        }

        await FormReader._reveal_dropdown_choices(page, fields, openable)

        logger.info(f"Read {len(fields)} fillable field(s) from {page.url}")

        return fields

    @staticmethod
    async def _reveal_dropdown_choices(
        page: Any,
        fields: List[FormField],
        openable: set,
    ) -> None:
        """
        Fill in the choices for dropdowns that only build their list on open.

        A custom select renders nothing until it is clicked, so its choices
        cannot be read from the static page. Presented to the user as a free
        text box, the question is a guess — and a form that only accepts its
        own wording rejects the answer. Opening each one is ordinary use of
        the page, the same thing a person does before choosing.

        Failures are ignored: an unopenable dropdown leaves the user typing an
        answer, which is where they were before.

        Args:
            page: The page holding the form
            fields: Fields read from it; enriched in place
            openable: Selectors of controls that are dropdowns in disguise
        """
        candidates = [
            field for field in fields
            if not field.options and field.selector in openable
        ]

        for field in candidates[:MAX_DROPDOWNS_PROBED]:
            try:
                # The stored selector is already CSS-escaped. Rebuilding one
                # from the raw id breaks on the ids these forms actually use:
                # "#4017901003" is invalid CSS because it starts with a digit,
                # so every demographic dropdown on the page was skipped and
                # arrived as a free-text box with no choices.
                element = page.locator(field.selector)

                if not await element.count():
                    continue

                # Only choices that appear *because* this control opened count.
                # Reading every option-ish element on the page would hand one
                # dropdown's list to an unrelated free-text question.
                before = await page.evaluate(_VISIBLE_CHOICES_JS)

                await element.first.click(timeout=DROPDOWN_OPEN_TIMEOUT_MS)

                after = await page.evaluate(_VISIBLE_CHOICES_JS)

                options = [choice for choice in after if choice not in before]

                # Close it again so the next click doesn't land inside this
                # dropdown's own list.
                await page.keyboard.press("Escape")

                unique = list(dict.fromkeys(options))

                # A long list is the normal shape of a country question, not a
                # sign something went wrong. Discarding it for its length left
                # the user typing a country name into a control that only
                # accepts one of its own 197.
                #
                # A list of *one* is not noise either: that is how these forms
                # render a consent — "Privacy notice" with the single choice
                # "Acknowledge/Confirm". Dropped, it reached the user as an
                # empty text box with no way to tell what accepting it meant.
                if unique:
                    field.options = unique[:MAX_CHOICES_PER_FIELD]
            except Exception as e:
                logger.debug(f"Could not read choices for {field.selector}: {e}")
                continue

    @staticmethod
    def _tidy(label: str) -> str:
        """
        Clean up label text scraped from the page.

        Labels routinely carry a trailing asterisk for "required" and line
        breaks from surrounding markup. Text is also repaired and normalized
        first — see _repair_mojibake and the note below.

        Args:
            label: Raw label text from the page

        Returns:
            Cleaned label
        """
        repaired = FormReader._repair_mojibake(label)

        # NFKC folds typographic variants onto their plain equivalents:
        # non-breaking spaces, curly quotes, ligatures. Sensitive-question
        # patterns are written in plain ASCII, so a label using a non-breaking
        # space would otherwise fail to match.
        normalized = unicodedata.normalize("NFKC", repaired)
        normalized = normalized.replace("\xa0", " ").translate(_QUOTE_FOLDING)

        cleaned = " ".join(normalized.split())

        return cleaned.rstrip("*: ").strip()

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        """
        Undo UTF-8 bytes that were decoded as cp1252.

        Career sites that omit a charset (and any meta tag) hand the browser
        text like "Are you a protected veteranâ€™s spouse?". The label is what
        sensitive-question matching reads, so a corrupted one can silently fail
        to match and the question would be treated as ordinary.

        The repair is only kept when it round-trips cleanly, so correctly
        encoded text — including text legitimately containing "Ã" — is left
        untouched.

        Args:
            text: Possibly mis-decoded text

        Returns:
            Repaired text, or the original when repair doesn't apply
        """
        if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
            return text

        try:
            repaired = text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

        # Only accept a repair that removed the tell-tale sequences
        if any(marker in repaired for marker in _MOJIBAKE_MARKERS):
            return text

        logger.debug(f"Repaired mis-encoded label: {text!r} -> {repaired!r}")

        return repaired
