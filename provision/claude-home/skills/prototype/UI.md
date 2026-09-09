# UI Prototype

Generate **several radically different UI variations** inside one self-contained HTML file, switchable from a floating bottom bar. The user flips between variants in the page, picks one (or steals bits from each), then throws the rest away.

If the question is about logic/state rather than what something looks like, this is the wrong branch. Use [LOGIC.md](LOGIC.md).

## When this is the right shape

- "What should this page look like?"
- "I want to see a few options for this dashboard before committing."
- "Try a different layout for the settings screen."
- Any time the user would otherwise spend a day picking between three vague mockups in their head.

## Why one file and not routes in the app

A prototype mounted in the running application is easier to judge because it sits among the real header, sidebar and data, but it needs a dev server and a browser on the machine that built it. The reviewer here may be looking at a file served from a machine with neither, on a phone. So the page recreates enough of the surrounding app to judge the variant against (a header strip, a sidebar outline, realistic density of sample data) and nothing more. Every variant lives in the same file, so what is being compared is the structure, not the chrome.

## Process

### 1. State the question and pick N

Default to **3 variants**. More than 5 stops being radically different and starts being noise, so cap there.

Write down the plan in one line at the top of the file, visible on the page:

> "Three variants of the settings page. Use the bar at the bottom or ←/→ to switch."

### 2. Generate radically different variants

Draft each variant. Hold each one to:

- The page's purpose and the data it has access to. Inline realistic sample data once and let every variant render it.
- The project's look: copy its colours, fonts, spacing and border radii into the page's `<style>` so a variant does not look foreign. Do not import the real component library; approximate it in plain CSS.
- A clear name, e.g. `A (Sidebar layout)`, `B (Cards)`, `C (Single column)`.

Variants must be **structurally different**: different layout, different information hierarchy, different primary affordance, not just different colours. Three slightly-tweaked card grids isn't a UI prototype, it's wallpaper. If two drafts come out too similar, redo one with explicit "do not use a card grid" guidance.

### 3. Wire them together

Each variant is one `<section data-variant="A">` (B, C, …) in the file. Only the section for the current variant is displayed. The current variant is stored in the URL hash (`#variant=B`) so a specific one can be linked and survives reload:

```html
<!-- pseudo-code, adapt freely -->
<section data-variant="A"> … </section>
<section data-variant="B"> … </section>
<section data-variant="C"> … </section>
<div id="switcher"><button data-dir="-1">←</button><span id="label"></span><button data-dir="1">→</button></div>
<script>
  const variants = [...document.querySelectorAll('[data-variant]')];
  const names = { A: 'Sidebar layout', B: 'Cards', C: 'Single column' };
  function current() { return (location.hash.match(/variant=(\w)/) || [, variants[0].dataset.variant])[1]; }
  function show(key) {
    variants.forEach(s => s.hidden = s.dataset.variant !== key);
    label.textContent = `${key} (${names[key]})`;
    if (current() !== key) location.hash = `variant=${key}`;
  }
  function cycle(dir) {
    const keys = variants.map(s => s.dataset.variant);
    show(keys[(keys.indexOf(current()) + dir + keys.length) % keys.length]);
  }
  document.querySelectorAll('#switcher button').forEach(b => b.onclick = () => cycle(+b.dataset.dir));
  addEventListener('keydown', e => {
    if (e.target.matches('input, textarea, [contenteditable]')) return;
    if (e.key === 'ArrowLeft') cycle(-1); if (e.key === 'ArrowRight') cycle(1);
  });
  addEventListener('hashchange', () => show(current()));
  show(current());
</script>
```

### 4. Build the floating switcher

A small fixed-position bar at the bottom-centre of the screen with three pieces:

- **Left arrow**: cycles to the previous variant (wraps around).
- **Variant label**: shows the current variant key and its name, e.g. `B (Cards)`.
- **Right arrow**: cycles forward (wraps around).

Behaviour:

- Clicking an arrow updates the URL hash so the variant is shareable and reload-stable.
- Keyboard: `←` and `→` arrow keys also cycle. Don't intercept arrow keys when an `<input>`, `<textarea>`, or `[contenteditable]` is focused.
- Touch: the arrows are at least 44px tall so the bar works on a phone.
- Visually distinct from the page (e.g. high-contrast pill, subtle shadow) so it's obviously not part of the design being evaluated.

### 5. Hand it over

Say where the file is and list the variant keys with their `#variant=` links. The user will flip through whenever they get to it. The interesting feedback is usually **"I want the header from B with the sidebar from C"**, which is the actual design they want.

### 6. Capture the answer and clean up

Once a variant has won, record the verdict the way [SKILL.md](SKILL.md) rule 6 describes: an issue comment naming the winner and why, and any parts borrowed from the others. Rebuild the winner properly in the real code with the project's real components; nothing is copied out of the prototype file. The file itself never lands on `main`.

## Anti-patterns

- **Variants that differ only in colour or copy.** That's a tweak, not a prototype. Real variants disagree about structure.
- **Sharing too much markup between variants.** A shared header strip is fine; a shared layout defeats the point. Each variant should be free to throw out the layout.
- **Loading anything from the network.** No CDN scripts, no web fonts, no images by URL. The file must render from disk with no connection.
- **Promoting the prototype directly to production.** The variant markup was written under prototype constraints (no components, no tests, no accessibility pass). Rebuild it properly when you fold it in.
