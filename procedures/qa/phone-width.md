# Checking a page at phone width

How a UI tester sees a page at phone width and finds every part of it that runs past the window's edge. The page's
own width can lie: a frame that hides what runs past it leaves the page as wide as the window while a bar is cut off.
So the check measures every visible part instead.

## 1. Set the width

Resize your browser to the phone width your brief gives, with the resize tool. Then read `window.innerWidth` with
the page-script tool. Another number than the one you asked for means the width did not take: report the step
blocked.

## 2. Define the check

Once per page load, run this with the page-script tool. It answers `"ready"`.

```js
() => {
  window.edgeCheck = () => {
    const w = innerWidth;
    // A row the product scrolls sideways on purpose carries data-scrolls-sideways. What is inside it may run past
    // the edge; the marked element itself must still fit. Over 1 px leaves out screen-reader text, and
    // checkVisibility leaves out closed menus.
    const past = [...document.body.querySelectorAll("*")].filter((e) => {
      const r = e.getBoundingClientRect();
      return r.width > 1 && r.height > 1 && (r.left < -0.5 || r.right > w + 0.5)
        && e.checkVisibility({ opacityProperty: true, visibilityProperty: true })
        && !e.parentElement.closest("[data-scrolls-sideways]");
    });
    const name = (e) => e.tagName.toLowerCase() + (typeof e.className === "string" && e.className.trim()
      ? "." + e.className.trim().split(/\s+/).join(".") : "");
    return { width: w, page: document.documentElement.scrollWidth, count: past.length,
      past: past.slice(0, 8).map((e) => { const r = e.getBoundingClientRect();
        return name(e) + " " + Math.round(r.left) + " to " + Math.round(r.right); }) };
  };
  return "ready";
}
```

A page that moves between screens without reloading keeps the check defined. A sign-in link, a reload or the
navigate tool loads a fresh page: define it again. If a run answers that `edgeCheck` is not defined, define it and
run once more; a second failure is reported blocked, with the error.

A new row that scrolls sideways on purpose gets `data-scrolls-sideways` from the implementer, with the reason in a
comment. A whole page container is never marked.

## 3. Wait for the screen

Before each run, wait until the screen's own content is drawn: the scenario names what to wait for. After opening a
menu or panel, wait for its motion to end. A page still loading answers clean, which is a false pass.

## 4. Run it and read it

Run `() => edgeCheck()` with the page-script tool. A screen fits when `page` equals `width` and `count` is 0.
Anything else is a finding: quote the answer in it. `past` names the first eight parts, each with where it starts
and ends; `count` says how many there are in all.

## 5. Put things back

When you finish, or stop early, resize to the first width your brief lists, and put the theme back if you changed
it. A width or a theme left over changes what the next scenario shows.
