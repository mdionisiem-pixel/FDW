// Small behaviours only: a working overlay for long runs, a signal filter,
// and the one-or-two rule on concept selection. Every rule that matters is
// enforced again on the server.
(function () {
  "use strict";

  // Sewing loaders. Each step that calls a model gets the loader that fits it;
  // anything else gets the crochet chain, which loops without a visible restart.
  var LOADER_BY_STEP = [
    ["/stage-1/", "spool"],
    ["/stage-2/", "hoop"],
    ["/stage-3/", "machine"],
    ["/bakeoff/", "cross"],
  ];
  function loaderFor(action) {
    for (var i = 0; i < LOADER_BY_STEP.length; i++) {
      if (action.indexOf(LOADER_BY_STEP[i][0]) !== -1) return LOADER_BY_STEP[i][1];
    }
    return "crochet";
  }
  function showLoader(name) {
    var loaders = document.querySelectorAll(".loader[data-loader]");
    var found = false;
    loaders.forEach(function (svg) {
      var match = svg.getAttribute("data-loader") === name;
      // SVG elements have no "hidden" property, so the attribute is set directly.
      svg.toggleAttribute("hidden", !match);
      found = found || match;
    });
    if (!found) showLoader("crochet");
  }

  // Build the repeated parts of two loaders once: the heart's X stitches and the chain loops.
  var SVG = "http://www.w3.org/2000/svg";
  var HEART = ["..XX.XX..", ".XXXXXXX.", ".XXXXXXX.", "..XXXXX..", "...XXX...", "....X...."];
  document.querySelectorAll(".ld-cross").forEach(function (group) {
    var n = 0;
    HEART.forEach(function (row, r) {
      row.split("").forEach(function (cell, c) {
        if (cell !== "X") return;
        var x = 24 + c * 9, y = 24 + r * 9;
        var stitch = document.createElementNS(SVG, "g");
        stitch.style.animationDelay = (n * 0.17) + "s";
        stitch.innerHTML =
          '<path d="M' + (x - 3.4) + ' ' + (y + 3.4) + 'L' + (x + 3.4) + ' ' + (y - 3.4) + '" stroke="#9b1848" stroke-width="2.4"/>' +
          '<path d="M' + (x - 3.4) + ' ' + (y - 3.4) + 'L' + (x + 3.4) + ' ' + (y + 3.4) + '" stroke="#c2255c" stroke-width="2.4"/>' +
          '<path d="M' + (x - 2.2) + ' ' + (y - 2.8) + 'L' + (x + 1.6) + ' ' + (y + 1) + '" stroke="#f3b7cc" stroke-width=".7"/>';
        group.appendChild(stitch);
        n++;
      });
    });
  });
  document.querySelectorAll(".ld-chain").forEach(function (group) {
    for (var k = 0; k < 14; k++) {
      var x = 20 + k * 16;
      var loop = document.createElementNS(SVG, "g");
      loop.innerHTML =
        '<ellipse cx="' + x + '" cy="60.6" rx="9.5" ry="6.2" fill="none" stroke="#7d1239" stroke-width="4.4"/>' +
        '<ellipse cx="' + x + '" cy="60" rx="9.5" ry="6.2" fill="none" stroke="#c2255c" stroke-width="3.2"/>' +
        '<path d="M' + (x - 6) + ' 55.4Q' + x + ' 53.2 ' + (x + 6) + ' 55.4" fill="none" stroke="#f3b7cc" stroke-width=".9"/>' +
        '<path d="M' + (x - 9) + ' 58l2 3M' + (x + 7) + ' 58l2 3M' + (x - 3) + ' 64l2 2" stroke="#9b1848" stroke-width=".8"/>';
      group.appendChild(loop);
    }
  });

  // Working overlay for forms that call the model.
  var overlay = document.getElementById("working");
  var overlayTitle = document.getElementById("working-title");
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      // The label can sit on the form or on the button that submitted it.
      var label = (event.submitter && event.submitter.getAttribute("data-working")) || form.getAttribute("data-working");
      if (!overlay || !label) return;
      overlayTitle.textContent = label;
      var action = (event.submitter && event.submitter.formAction) || form.action || "";
      showLoader((event.submitter && event.submitter.getAttribute("data-loader")) || form.getAttribute("data-loader") || loaderFor(action));
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

  // Image test: make waiting images one request at a time, so a slow model
  // never holds one long request open, and progress is visible.
  var make = document.getElementById("make");
  var makeButton = document.getElementById("make-button");
  if (make && makeButton) {
    var token = make.querySelector("input[name=csrfmiddlewaretoken]").value;
    var fill = document.getElementById("make-fill");
    var status = document.getElementById("make-status");
    var show = function (p) {
      var finished = p.done + p.failed;
      fill.style.width = (p.total ? Math.round(finished / p.total * 100) : 0) + "%";
      status.textContent = p.done + " made, " + p.failed + " failed, " + p.waiting + " waiting.";
    };
    var step = function () {
      fetch(make.getAttribute("data-url"), {
        method: "POST",
        headers: { "X-CSRFToken": token, "X-Requested-With": "fetch" },
        credentials: "same-origin"
      }).then(function (response) {
        return response.json().then(function (body) { return { ok: response.ok, body: body }; });
      }).then(function (result) {
        if (result.body.progress) show(result.body.progress);
        if (!result.ok) {
          status.textContent = result.body.error || "Generation stopped.";
          makeButton.disabled = false;
          makeButton.textContent = "Continue";
          return;
        }
        if (result.body.finished) {
          status.textContent += " Done. Loading the images.";
          window.location.reload();
          return;
        }
        step();
      }).catch(function () {
        status.textContent = "The connection dropped. Press Continue to carry on; nothing made so far is lost.";
        makeButton.disabled = false;
        makeButton.textContent = "Continue";
      });
    };
    makeButton.addEventListener("click", function () {
      makeButton.disabled = true;
      makeButton.textContent = "Making images";
      step();
    });
  }

  // Image test: save scores without leaving the page.
  document.querySelectorAll(".score-form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var saved = form.querySelector(".score-saved");
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin"
      }).then(function (response) { return response.json(); }).then(function (body) {
        saved.textContent = body.ok ? "Saved" : (body.error || "Not saved");
        var card = form.closest(".score-card");
        if (card) card.classList.toggle("is-scored", !!body.ok);
      }).catch(function () { saved.textContent = "Not saved. Check the connection."; });
    });
  });
})();
