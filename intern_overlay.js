// Day One overlay, injected with page.add_init_script() into the browser the agent drives.
// Python drives it with page.evaluate:
//   window.__dayone.mark({ mode: "memory" | "reasoning" | "moved", selector?, bbox?: [x, y, w, h], label? })
//   window.__dayone.clear()
// Lives in a shadow root so the host page's CSS cannot touch it, and vice versa.
(() => {
  if (window.__dayone) return;

  const MODES = {
    memory: { color: "#16a34a", text: "From memory" },
    reasoning: { color: "#d97706", text: "Reasoning" },
    moved: { color: "#dc2626", text: "This moved." },
  };
  const RING_PADDING_PX = 6;
  const FOLLOW_INTERVAL_MS = 120;

  let root = null;
  let ring = null;
  let tag = null;
  let follow = null;

  function mount() {
    if (root) return;
    const host = document.createElement("div");
    host.setAttribute("data-dayone-overlay", "");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
    root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        .ring {
          position: fixed; box-sizing: border-box; border-radius: 10px;
          border: 3px solid var(--c); box-shadow: 0 0 0 4px color-mix(in srgb, var(--c) 25%, transparent);
          opacity: 0; transform: scale(1.04);
          transition: opacity 160ms ease-out, transform 160ms ease-out,
                      left 160ms ease-out, top 160ms ease-out, width 160ms ease-out, height 160ms ease-out;
        }
        .ring.on { opacity: 1; transform: scale(1); }
        .tag {
          position: absolute; left: -3px; bottom: calc(100% + 6px); white-space: nowrap;
          font: 600 13px/1 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
          color: #fff; background: var(--c); padding: 6px 9px; border-radius: 6px;
        }
        .ring.below .tag { bottom: auto; top: calc(100% + 6px); }
        .ring.inside .tag { bottom: auto; top: 10px; left: 10px; font-size: 18px; padding: 10px 14px; }
        @media (prefers-reduced-motion: reduce) { .ring { transition: opacity 120ms linear; transform: none; } }
      </style>
      <div class="ring"><span class="tag"></span></div>`;
    ring = root.querySelector(".ring");
    tag = root.querySelector(".tag");
    document.documentElement.appendChild(host);
    ring.getBoundingClientRect(); // flush styles so the first mark transitions in instead of popping
  }

  function place(rect) {
    const x = rect.x - RING_PADDING_PX;
    const y = rect.y - RING_PADDING_PX;
    ring.style.left = `${x}px`;
    ring.style.top = `${y}px`;
    ring.style.width = `${rect.width + RING_PADDING_PX * 2}px`;
    ring.style.height = `${rect.height + RING_PADDING_PX * 2}px`;
    ring.classList.toggle("below", y < 40);
  }

  function rectFor({ selector, bbox }) {
    if (selector) {
      const el = document.querySelector(selector);
      if (el) return el.getBoundingClientRect();
    }
    if (bbox) {
      // bbox is in page coordinates (Playwright bounding_box at scroll 0); convert to viewport.
      return new DOMRect(bbox[0] - window.scrollX, bbox[1] - window.scrollY, bbox[2], bbox[3]);
    }
    return null;
  }

  function stopFollowing() {
    if (follow) clearInterval(follow);
    follow = null;
  }

  function mark(options) {
    const mode = MODES[options.mode];
    if (!mode) throw new Error(`unknown overlay mode: ${options.mode}`);
    mount();
    stopFollowing();
    ring.style.setProperty("--c", mode.color);
    tag.textContent = options.label ? `${mode.text} · ${options.label}` : mode.text;
    const rect = rectFor(options);
    // No target (e.g. the saved locator matched nothing): show the tag over the whole viewport.
    place(rect ?? new DOMRect(8, 8, window.innerWidth - 16, window.innerHeight - 16));
    ring.classList.toggle("inside", !rect);
    ring.classList.add("on");
    if (options.selector) {
      follow = setInterval(() => {
        const next = rectFor(options);
        if (next) place(next);
      }, FOLLOW_INTERVAL_MS);
    }
  }

  function clear() {
    stopFollowing();
    if (ring) ring.classList.remove("on");
  }

  window.__dayone = { mark, clear };
})();
