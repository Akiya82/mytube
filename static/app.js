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
