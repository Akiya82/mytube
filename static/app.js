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
// navigator.clipboard exists only in a secure context, and the documented
// way to widen this interface -- MYTUBE_UI_BIND on a LAN address -- is
// reached over plain http, which is not one. Reading .writeText off an
// undefined clipboard throws, so this screen's headline control would do
// nothing at all with no message. Two fallbacks behind it: a hidden
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

// Preparing-row poll (D-49, 02-UI-SPEC.md States -> Preparing): every 4
// seconds, fetch a row's fragment route and replace only that row's
// element -- never the page, so scroll position and focus elsewhere
// survive. Stops once the returned row is no longer "preparing".
function pollLibraryRow(row) {
  const titleId = row.getAttribute("data-title-id");
  fetch(`/library/${titleId}/row`)
    .then((res) => (res.ok ? res.text() : null))
    .then((html) => {
      const template = document.createElement("template");
      template.innerHTML = html === null ? "" : html.trim();
      const fresh = template.content.firstElementChild;
      if (!fresh) {
        // 404 or an empty body: the row was dismissed or purged from
        // another tab. replaceWith stringifies its argument, so the null
        // firstElementChild would leave a text node reading "null" here.
        row.remove();
        return;
      }
      row.replaceWith(fresh);
      if (fresh.getAttribute("data-row-status") === "preparing") {
        setTimeout(() => pollLibraryRow(fresh), 4000);
      }
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
document.addEventListener("submit", (event) => {
  const message = event.target.getAttribute("data-confirm");
  if (message && !confirm(message)) {
    event.preventDefault();
  }
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
