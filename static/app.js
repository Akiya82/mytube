// Hand-written browser behaviour for this phase -- no HTMX, no client
// framework (see 02-02-PLAN.md "Decisions this plan makes, that later
// plans inherit"). Delegated listeners so elements added by a later plan's
// server-rendered fragment swap work without re-binding anything.
//
// Copy-to-clipboard: reads the address from data-copy-text on the clicked
// element, writes it to the clipboard, swaps the button label to the
// "copied" text for two seconds. Later plans append the delete countdown
// tick and the status poll to this same file.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-text]");
  if (!button) return;

  const text = button.getAttribute("data-copy-text");
  const doneLabel = button.getAttribute("data-copy-done-label") || button.textContent;
  const originalLabel = button.textContent;

  navigator.clipboard.writeText(text).then(() => {
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
    .then((res) => res.text())
    .then((html) => {
      const template = document.createElement("template");
      template.innerHTML = html.trim();
      const fresh = template.content.firstElementChild;
      row.replaceWith(fresh);
      if (fresh.getAttribute("data-row-status") === "preparing") {
        setTimeout(() => pollLibraryRow(fresh), 4000);
      }
    });
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
