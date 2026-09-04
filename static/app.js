// Hand-written browser behaviour for this phase -- no HTMX, no client
// framework (see 02-02-PLAN.md "Decisions this plan makes, that later
// plans inherit"). Delegated listeners so elements added by a later plan's
// server-rendered fragment swap work without re-binding anything.
//
// Copy-to-clipboard: reads the address from data-copy-text on the clicked
// element, writes it to the clipboard, swaps the button label to the
// "copied" text for two seconds. Later plans append the delete countdown
// tick and the status poll to this same file.
//
// navigator.clipboard exists only in a secure context, and the interface
// is reached over the operator's own TLS from here on -- but a secure
// context is not guaranteed regardless (a self-signed cert an operator's
// browser has not trusted yet, for one), so the fallback stays: it costs
// nothing, and reading .writeText off an undefined clipboard throws, so
// this screen's headline control would do nothing at all with no message
// if it were removed. Two fallbacks behind it: a hidden
// textarea with the legacy copy command, then a prompt holding the address
// so the operator can at least select it by hand.
function copyTextToClipboard(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "0";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (err) {
    copied = false;
  }
  area.remove();
  if (!copied) window.prompt("", text);
  return copied;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-text]");
  if (!button) return;

  const text = button.getAttribute("data-copy-text");
  const doneLabel = button.getAttribute("data-copy-done-label") || button.textContent;
  const originalLabel = button.textContent;

  const written = navigator.clipboard
    ? navigator.clipboard.writeText(text).then(
        () => true,
        () => copyTextToClipboard(text)
      )
    : Promise.resolve(copyTextToClipboard(text));

  written.then((ok) => {
    if (!ok) return;
    button.textContent = doneLabel;
    setTimeout(() => {
      button.textContent = originalLabel;
    }, 2000);
  });
});

// Preparing-row poll (D-49, 02-UI-SPEC.md States -> Preparing; 04-UI-
// SPEC.md Amendment 2): every 4 seconds, fetch a row's own fragment
// address and replace only that row's element -- never the page, so
// scroll position and focus elsewhere survive. Stops once the returned
// row is no longer "preparing". The address comes off the row's own
// data-row-url attribute rather than being built from a title id here --
// a video row and a playlist row have two different address shapes, and
// the row itself is the only thing that knows which one it is.
function pollLibraryRow(row) {
  const url = row.getAttribute("data-row-url");
  fetch(url)
    .then((res) => {
      // Fact: a dead session met the gate, fetch followed its 302 to
      // /login, and res.ok is true for a login page, not a row -- reload
      // and stop, touching neither this row nor the poll timer.
      if (res.redirected) return window.location.reload();
      // Fact: a plain 404, or a 200 with an empty body -- the row was
      // dismissed or purged elsewhere.
      if (!res.ok) return row.remove();
      return res.text().then((html) => {
        const template = document.createElement("template");
        template.innerHTML = html.trim();
        const fresh = template.content.firstElementChild;
        if (!fresh) return row.remove();
        row.replaceWith(fresh);
        if (fresh.getAttribute("data-row-status") === "preparing") {
          setTimeout(() => pollLibraryRow(fresh), 4000);
        }
      });
    })
    // One dropped request must not silently end the poll -- the row would
    // sit on "Working..." until a manual reload.
    .catch(() => setTimeout(() => pollLibraryRow(row), 4000));
}

document
  .querySelectorAll('.library-row[data-row-status="preparing"]')
  .forEach((row) => setTimeout(() => pollLibraryRow(row), 4000));

// Delete/undo/purge confirmation (D-62): a native confirm() dialog, the
// deliberately minimal mechanism -- keyboard- and screen-reader-operable
// for free, no custom modal. A form carrying data-confirm blocks its own
// submit unless the operator accepts the dialog.
//
// data-submit-once (the create-password form): the setup token is
// consumed the instant the first submission succeeds, so a double-click
// sends a second POST carrying a now-spent token -- the browser abandons
// the first navigation and shows the second response, and the operator
// ends up looking at "this link is no longer valid" seconds after their
// account was in fact created. Disabling the button on submit is the fix;
// nothing on the server can tell that second request apart from a
// stranger replaying the token.
document.addEventListener("submit", (event) => {
  const message = event.target.getAttribute("data-confirm");
  if (message && !confirm(message)) return event.preventDefault();
  if (event.target.hasAttribute("data-submit-once"))
    event.target.querySelector('[type="submit"]').disabled = true;
});

// Delete countdown tick (D-62): computed every second from the
// server-rendered absolute data-deadline (ms since epoch) on the row --
// never polled from the server, so a reload shows the true remaining time
// instead of restarting the clock.
function tickCountdown(row) {
  const chip = row.querySelector(".chip--deleting");
  if (!chip) return;
  const remaining = Math.max(0, Math.round((Number(row.getAttribute("data-deadline")) - Date.now()) / 1000));
  const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
  const ss = String(remaining % 60).padStart(2, "0");
  chip.textContent = chip.getAttribute("data-countdown-template").replace("{mm}", mm).replace("{ss}", ss);
  if (remaining > 0) setTimeout(() => tickCountdown(row), 1000);
}

document
  .querySelectorAll('.library-row[data-row-status="pending_delete"]')
  .forEach((row) => tickCountdown(row));

// Playlist reorder: drag-and-drop layered on top of the required up/down
// arrows (D-107) -- hand-written against native drag events, no library.
// A drop fires the same one-step move request an arrow's own form
// submits, repeated once per position crossed (T-03-20): one server-side
// reorder path, not a client-computed order posted wholesale.
let _draggedItemId = null;

document.addEventListener("dragstart", (event) => {
  const row = event.target.closest('.member-row[draggable="true"]');
  if (row) _draggedItemId = row.getAttribute("data-item-id");
});

// A drag the operator abandons (Escape, or a drop outside the list) never
// reaches the drop handler that would otherwise clear this, and a stale id
// left behind would let the next unrelated drop -- a file dragged in from
// the desktop, say -- fire a move for a row nobody touched.
document.addEventListener("dragend", () => {
  _draggedItemId = null;
});

document.addEventListener("dragover", (event) => {
  if (_draggedItemId && event.target.closest(".member-row")) event.preventDefault();
});

document.addEventListener("drop", (event) => {
  const targetRow = event.target.closest(".member-row");
  const list = targetRow && targetRow.closest(".member-list");
  const itemId = _draggedItemId;
  _draggedItemId = null;
  if (!targetRow || !list || !itemId) return;
  event.preventDefault();

  const rows = Array.from(list.querySelectorAll(".member-row"));
  const from = rows.findIndex((row) => row.getAttribute("data-item-id") === itemId);
  const to = rows.indexOf(targetRow);
  if (from === -1 || to === -1 || from === to) return;

  const playlistId = list.getAttribute("data-playlist-id");
  reorderPlaylistMember(playlistId, itemId, to > from ? "down" : "up", Math.abs(to - from)).catch(() => {});
});

// The move POST always redirects (D-107's baseline), success included --
// res.redirected is true either way, so a dead session is told apart by
// WHERE it landed (login) rather than whether it redirected at all. A
// request that fails outright, or lands on login, reloads rather than
// leaving the drag's visual position uncorrected: nothing here ever
// reorders the DOM directly, so there is nothing else to roll back.
async function reorderPlaylistMember(playlistId, itemId, direction, steps) {
  for (let step = 0; step < steps; step++) {
    const res = await fetch(`/playlists/${playlistId}/items/${itemId}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `direction=${direction}`,
    });
    if (!res.ok || new URL(res.url).pathname === "/login") return window.location.reload();
  }

  // Refetch the member fragment (a GET, safe to re-fetch/reload) and
  // install only its order section -- the fragment route never queries
  // the picker, so swapping in more than that would blank the picker
  // already on the page.
  const res = await fetch(`/playlists/${playlistId}/items`);
  if (!res.ok || new URL(res.url).pathname === "/login") return window.location.reload();
  const template = document.createElement("template");
  template.innerHTML = (await res.text()).trim();
  const fresh = template.content.querySelector(".playlist-order");
  const current = document.querySelector(".playlist-order");
  if (fresh && current) current.replaceWith(fresh);
}

// The on-demand preview (04-UI-SPEC.md States -> Preview): two delegated
// click handlers, same disable-and-swap-the-label mechanism the copy-link
// button already uses above, extended with a second mechanism this screen
// needs -- replacing only the .preview-panel element with the fragment
// the render route answers, never the whole Conversion block or the page.
//
// Neither handler reads the subtitle/layout choice from the panel itself
// -- the panel is swapped out on every render, so anything it carried
// would not survive a second click. Both read straight off the
// surrounding form's own radio groups and text field at click time,
// exactly the values a normal submit of that same form would send.
function _previewFormValues(form) {
  const subtitleChoice = form.querySelector('[name="subtitle_choice"]:checked');
  const layout = form.querySelector('[name="stereo_layout"]:checked');
  const path = form.querySelector("[data-preview-panel]");
  const body = new URLSearchParams();
  body.set("subtitle_choice", subtitleChoice ? subtitleChoice.value : "none");
  body.set(
    "subtitle_manual_path",
    (form.querySelector('[name="subtitle_manual_path"]') || {}).value || ""
  );
  body.set("stereo_layout", layout ? layout.value : "2d");
  return { body, path: path ? path.getAttribute("data-preview-path") : "" };
}

async function _requestPreview(button, form, after) {
  const panel = form.querySelector("[data-preview-panel]");
  if (!panel) return;
  const { body, path } = _previewFormValues(form);
  body.set("after", after);

  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = button.getAttribute("data-preview-loading-label") || originalLabel;

  // Encoded segment by segment, never the whole path at once -- encoding
  // the "/" separators too would stop the route from matching at all. A
  // name containing "#", "?" or "%" would otherwise cut the request path
  // short or be misread as a query string.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const res = await fetch(`/preview/${encodedPath}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  }).finally(() => {
    button.disabled = false;
    button.textContent = originalLabel;
  });
  if (!res.ok) return;
  const template = document.createElement("template");
  template.innerHTML = (await res.text()).trim();
  const fresh = template.content.firstElementChild;
  if (fresh) panel.replaceWith(fresh);
}

// One delegated listener for both controls -- "Preview subtitles" always
// starts at 0; "Try another frame" reads the currently shown frame's own
// timestamp off its sibling <img>, one panel up from wherever inside it
// the click landed.
document.addEventListener("click", (event) => {
  const renderButton = event.target.closest("[data-preview-render]");
  const anotherButton = event.target.closest("[data-preview-another]");
  const button = renderButton || anotherButton;
  if (!button) return;
  const form = button.closest("form");
  if (!form) return;

  let after = "0";
  if (anotherButton) {
    const panel = button.closest("[data-preview-panel]");
    const frame = panel && panel.querySelector("[data-frame-seconds]");
    after = frame ? frame.getAttribute("data-frame-seconds") : "0";
  }

  _requestPreview(button, form, after);
});
