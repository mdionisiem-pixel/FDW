// Small behaviours only: a working overlay for long runs, a signal filter,
// and the one-or-two rule on concept selection. Every rule that matters is
// enforced again on the server.
(function () {
  "use strict";

  // Working overlay for forms that call the model.
  var overlay = document.getElementById("working");
  var overlayTitle = document.getElementById("working-title");
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      // The label can sit on the form or on the button that submitted it.
      var label = (event.submitter && event.submitter.getAttribute("data-working")) || form.getAttribute("data-working");
      if (!overlay || !label) return;
      overlayTitle.textContent = label;
      overlay.hidden = false;
      form.querySelectorAll("button[type=submit]").forEach(function (button) {
        // Disable after the submit has been dispatched, so the form still posts.
        setTimeout(function () { button.disabled = true; }, 0);
      });
    });
  });
  // If the page is restored from the back/forward cache, hide the overlay.
  window.addEventListener("pageshow", function () {
    if (overlay) overlay.hidden = true;
  });

  // Filter extracted signals by intent.
  var cells = document.querySelectorAll(".tally-cell[data-filter]");
  var table = document.getElementById("signals-table");
  cells.forEach(function (cell) {
    cell.addEventListener("click", function () {
      var filter = cell.getAttribute("data-filter");
      cells.forEach(function (c) { c.classList.toggle("is-active", c === cell); });
      if (!table) return;
      table.querySelectorAll("tbody tr").forEach(function (row) {
        row.hidden = filter !== "all" && row.getAttribute("data-intent") !== filter;
      });
      var fold = table.closest("details");
      if (fold) fold.open = true;
    });
  });

  // Concept selection: one or two.
  var selectForm = document.getElementById("select-form");
  if (selectForm) {
    var boxes = selectForm.querySelectorAll("input[name=concepts]");
    var button = document.getElementById("select-button");
    var hint = document.getElementById("select-hint");
    var update = function () {
      var picked = selectForm.querySelectorAll("input[name=concepts]:checked").length;
      boxes.forEach(function (box) { box.disabled = picked >= 2 && !box.checked; });
      if (button) button.disabled = picked < 1;
      if (hint) {
        hint.textContent = picked === 0
          ? "Pick one or two concepts above."
          : picked + " picked. " + (picked === 1 ? "You can add one more." : "That is the maximum.");
      }
    };
    boxes.forEach(function (box) { box.addEventListener("change", update); });
    update();
  }
})();
