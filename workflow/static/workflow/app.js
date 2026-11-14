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

  // A light line under every loader, changed every few seconds, so a long
  // wait reads as work in progress rather than a frozen page. Decorative only:
  // the real status is in the title and the progress line.
  var QUIPS = {
    research: [
      "Reading the trend reports so you don't have to",
      "Eavesdropping politely on the front row",
      "Asking the internet what it wants to wear",
      "Separating real demand from influencer noise",
      "Counting how many times 'quiet luxury' was said",
      "Checking every number has a source"
    ],
    design: [
      "Sketching on the back of a napkin",
      "Arguing about the palette",
      "Pinning swatches to the mood board",
      "Giving each collection a signature detail",
      "Measuring twice, cutting once",
      "The reviewer is being picky, as asked"
    ],
    sourcing: [
      "Reading the suppliers' small print",
      "Doing the margin maths with a sharp pencil",
      "Checking who can actually knit this",
      "Converting dollars without rounding in our favour",
      "Asking whether four yarn colours is really enough"
    ],
    images: [
      "Steaming out the creases before the shoot",
      "Adjusting the studio lights",
      "Asking the model to relax her shoulders",
      "Holding the swatch next to the screen",
      "The reviewer is counting stitches. Again.",
      "Redrawing the cuff the reviewer did not like",
      "Making sure nobody grew a sixth finger"
    ],
    review: [
      "Putting on the reading glasses",
      "Holding each image up to the window",
      "Comparing notes with your scores",
      "Squinting at the colour of that red",
      "Being strict, as instructed"
    ],
    analyst: [
      "Reading between the lines",
      "Asking why, one more time",
      "Measuring twice before cutting",
      "Turning 'premium' into a price",
      "Sorting vision from wishful thinking",
      "Pinning goals to dates",
      "Comparing notes with your brief"
    ],
    models: [
      "Asking the haberdasher what is in stock",
      "Counting the needles in the drawer",
      "Reading the price tags",
      "Checking which machines are plugged in",
      "Sorting the threads by colour"
    ],
    general: [
      "Threading the needle",
      "Untangling a stubborn knot",
      "Pressing the seams flat",
      "Finding the end of the yarn",
      "Pinning the pattern in place"
    ]
  };
  var QUIPS_BY_STEP = [
    ["/stage-1/", "research"],
    ["/stage-2/", "design"],
    ["/stage-3/", "sourcing"],
    ["/stage-4/", "images"],
    ["/bakeoff/reviewer/", "review"],
    ["/check-models/", "models"],
    ["/brand-brief/", "analyst"],
    ["/bakeoff/", "images"]
  ];
  function quipsFor(action) {
    for (var i = 0; i < QUIPS_BY_STEP.length; i++) {
      if (action.indexOf(QUIPS_BY_STEP[i][0]) !== -1) return QUIPS_BY_STEP[i][1];
    }
    return "general";
  }
  var REDUCED_MOTION = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Rotate the captions. Each change fades out, swaps the text and fades back in,
  // and the line keeps a fixed height, so nothing around it moves.
  function startQuips(line, key) {
    var list = QUIPS[key] || QUIPS.general;
    var index = Math.floor(Math.random() * list.length);
    var swapTimer = null;
    var put = function () {
      var text = list[index % list.length] + "\u2026";
      index += 1;
      if (REDUCED_MOTION || !line.textContent) { line.textContent = text; return; }
      line.classList.add("is-swapping");
      swapTimer = setTimeout(function () {
        line.textContent = text;
        line.classList.remove("is-swapping");
      }, 260);
    };
    put();
    var timer = setInterval(put, 4500);
    return function () {
      clearInterval(timer);
      clearTimeout(swapTimer);
      line.classList.remove("is-swapping");
      line.textContent = "";
    };
  }
  function duration(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    var minutes = Math.floor(seconds / 60);
    if (minutes >= 60) return Math.floor(minutes / 60) + " h " + (minutes % 60) + " min";
    return minutes ? minutes + " min " + (seconds % 60) + " s" : seconds + " s";
  }
  function stamp(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var s = seconds % 60;
    return Math.floor(seconds / 60) + ":" + (s < 10 ? "0" : "") + s;
  }
  // Keep the newest few lines in a feed list, newest first.
  function pushFeed(feed, text, at, failed, keep) {
    if (!feed || !text) return;
    var empty = feed.querySelector(".work-feed-empty");
    if (empty) empty.remove();
    var item = document.createElement("li");
    if (failed) item.className = "is-failed";
    var time = document.createElement("span");
    time.className = "work-feed-time";
    time.textContent = stamp(at);
    item.appendChild(time);
    item.appendChild(document.createTextNode(" " + text));
    item.title = text;
    feed.insertBefore(item, feed.firstChild);
    while (feed.children.length > (keep || 3)) feed.removeChild(feed.lastChild);
  }
  // The in-panel work card: a sewing loader, a caption, live numbers and the
  // last few results. Loops call update() with their progress and note() with
  // each finished item; the card works out the pace and the time left.
  function inlineLoader(panel) {
    var box = panel && panel.querySelector(".inline-working");
    var nothing = function () {};
    if (!box) return { start: nothing, stop: nothing, update: nothing, set: nothing, note: nothing };
    var stage = box.querySelector(".inline-loaders");
    var line = box.querySelector(".working-quip");
    var feed = box.querySelector(".work-feed");
    var thread = box.querySelector(".work-thread");
    var stat = function (name) { return box.querySelector('[data-stat="' + name + '"]'); };
    // A compact card shows only some numbers; the others are skipped.
    var put = function (name, text) { var cell = stat(name); if (cell) cell.textContent = text; };
    var stopQuips = null;
    var ticker = null;
    var started = 0;
    var firstFinished = null;
    var finished = 0;
    var total = 0;
    var render = function () {
      var elapsed = (Date.now() - started) / 1000;
      var made = firstFinished === null ? 0 : finished - firstFinished;
      var left = Math.max(0, total - finished);
      put("elapsed", duration(elapsed));
      put("done", total ? finished + " of " + total : "\u2013");
      if (thread) thread.style.setProperty("--done", total ? Math.min(100, finished / total * 100) + "%" : "0%");
      if (made > 0 && elapsed > 0) {
        var each = elapsed / made;
        put("pace", duration(each) + " each");
        put("eta", left ? "about " + duration(each * left) : "finishing");
      } else {
        put("pace", "measuring");
        put("eta", "measuring");
      }
    };
    return {
      start: function () {
        if (!stage.firstElementChild) {
          var name = panel.getAttribute("data-loader") || "crochet";
          var source = document.querySelector('#working .loader[data-loader="' + name + '"]') ||
            document.querySelector('#working .loader[data-loader="crochet"]');
          if (source) {
            var copy = source.cloneNode(true);
            copy.removeAttribute("hidden");
            stage.appendChild(copy);
          }
        }
        started = Date.now();
        // Measure the pace from what was already finished when this run began.
        var bar = panel.querySelector("[role=progressbar]");
        if (bar) {
          finished = parseInt(bar.getAttribute("aria-valuenow"), 10) || 0;
          total = parseInt(bar.getAttribute("aria-valuemax"), 10) || 0;
        }
        firstFinished = finished;
        box.hidden = false;
        panel.classList.add("is-running");
        if (!stopQuips) stopQuips = startQuips(line, panel.getAttribute("data-quips") || "general");
        render();
        clearInterval(ticker);
        ticker = setInterval(render, 1000);
      },
      stop: function () {
        box.hidden = true;
        panel.classList.remove("is-running");
        clearInterval(ticker);
        ticker = null;
        if (stopQuips) { stopQuips(); stopQuips = null; }
      },
      update: function (done, all) {
        if (firstFinished === null || done < firstFinished) firstFinished = done;
        finished = done;
        total = all;
        if (started) render();
      },
      set: function (name, text) {
        var cell = stat(name);
        if (cell) cell.textContent = text;
      },
      note: function (text, failed) {
        pushFeed(feed, text, (Date.now() - started) / 1000, failed);
      }
    };
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

  // Live progress while a stage runs: finished model calls, the last step and
  // the time so far. Each call is recorded when it finishes, so this is real.
  var STEP_LABELS = {
    extract_signals: "Read your audience messages",
    research_trends: "Searched the market",
    reformat_findings: "Tidied the research findings",
    demand_brief: "Wrote the market report",
    plan_concepts: "Planned the three collections",
    detail_concept: "Specified a collection",
    review_concept: "Reviewed a collection",
    revise_concept: "Revised a collection after review",
    sourcing_check: "Checked the pieces against your suppliers"
  };
  function stepLabel(step) {
    if (/_retry$/.test(step)) return "Corrected an answer that did not pass checks";
    return STEP_LABELS[step] || "Finished a step";
  }
  // Live numbers in the working overlay. Pages that log model calls (the cycle
  // page) also list each finished step; other forms show the elapsed time only.
  function startProgress() {
    var stats = document.getElementById("working-stats");
    if (!stats) return;
    var feed = document.getElementById("working-feed");
    var source = document.querySelector("[data-progress-url]");
    var tracked = Boolean(source && window.fetch);
    var stat = function (name) { return stats.querySelector('[data-stat="' + name + '"]'); };
    document.querySelectorAll("#working .working-steps").forEach(function (el) { el.hidden = !tracked; });
    stats.classList.toggle("is-single", !tracked);
    var started = Date.now();
    var shown = 0;
    function render() { stat("elapsed").textContent = duration((Date.now() - started) / 1000); }
    render();
    setInterval(render, 1000);
    if (!tracked) return;
    var url = source.getAttribute("data-progress-url") + "?after=" + encodeURIComponent(source.getAttribute("data-progress-after") || "0");
    setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || !data.calls) return;
          var calls = data.calls;
          for (; shown < calls.length; shown += 1) {
            var call = calls[shown];
            pushFeed(feed, stepLabel(call.step) + (call.ok ? "" : " (failed)"), (Date.now() - started) / 1000, !call.ok);
          }
          stat("steps").textContent = calls.length;
          if (calls.length) stat("last").textContent = stepLabel(calls[calls.length - 1].step);
        })
        .catch(function () { /* a missed poll is harmless; the next one catches up */ });
    }, 4000);
  }

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
      var quipLine = document.getElementById("working-quip");
      if (quipLine) startQuips(quipLine, form.getAttribute("data-quips") || quipsFor(action));
      startProgress();
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
    var makeLoader = inlineLoader(make);
    var busyLabel = make.getAttribute("data-busy-label") || "Making images";
    var doneWord = make.getAttribute("data-done-word") || "made";
    // In the reviewer check the mark that matters is disagreement with the founder.
    var badResult = new RegExp(make.getAttribute("data-bad-result") || "failed|could not");
    var fill = document.getElementById("make-fill");
    var status = document.getElementById("make-status");
    var makeBar = make.querySelector("[role=progressbar]");
    var show = function (p) {
      var finished = p.done + p.failed;
      fill.style.width = (p.total ? Math.round(finished / p.total * 100) : 0) + "%";
      if (makeBar) makeBar.setAttribute("aria-valuenow", finished);
      status.textContent = p.done + " " + doneWord + ", " + p.failed + " failed, " + p.waiting + " waiting.";
      makeLoader.update(finished, p.total);
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
        if (result.body.did) makeLoader.note(result.body.did, badResult.test(result.body.did));
        if (!result.ok) {
          status.textContent = result.body.error || "Generation stopped.";
          makeLoader.stop();
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
        makeLoader.stop();
        makeButton.disabled = false;
        makeButton.textContent = "Continue";
      });
    };
    makeButton.addEventListener("click", function () {
      makeButton.disabled = true;
      makeButton.textContent = busyLabel;
      makeLoader.start();
      step();
    });
  }

  // Open points: pick up to three options; the line to be saved is composed below, ready to edit.
  // The composition matches the server's, so saving without editing gives the same line.
  document.querySelectorAll("[data-option-form]").forEach(function (form) {
    var own = form.querySelector("textarea[name=own]");
    var max = parseInt(form.getAttribute("data-max"), 10) || 3;
    var error = form.querySelector("[data-choice-error]");
    var boxes = form.querySelectorAll("input[name=choice]");
    var compose = function () {
      var chosen = Array.prototype.filter.call(boxes, function (b) { return b.checked; });
      boxes.forEach(function (b) { b.disabled = !b.checked && chosen.length >= max; });
      if (error) error.hidden = chosen.length < max;
      if (error && chosen.length >= max) error.textContent = "That is the most you can combine. Untick one to choose another.";
      if (!chosen.length) { own.value = ""; return; }
      if (chosen.length === 1) { own.value = chosen[0].value; return; }
      var first = chosen[0].value;
      var subject = first.indexOf(":") > -1 ? first.split(":")[0].trim() : first;
      var parts = chosen.map(function (b) {
        var text = b.value;
        var body = text.indexOf(":") > -1 ? text.slice(text.indexOf(":") + 1).trim() : text;
        return b.getAttribute("data-label") + ": " + body.replace(/\.+$/, "");
      });
      own.value = subject + ", " + chosen.length + " combined directions. " + parts.join(" | ") + ".";
    };
    boxes.forEach(function (b) { b.addEventListener("change", compose); });
  });

  // Goal rows: "Add another goal" copies an empty row.
  document.querySelectorAll("[data-goal-rows]").forEach(function (set) {
    var add = set.querySelector("[data-add-goal]");
    if (!add) return;
    add.addEventListener("click", function () {
      var rows = set.querySelectorAll(".goal-row:not(.goal-row-head)");
      var copy = rows[rows.length - 1].cloneNode(true);
      copy.querySelectorAll("input").forEach(function (input) { input.value = ""; });
      add.parentNode.insertBefore(copy, add);
      copy.querySelector("input").focus();
    });
  });

  // Brand interview: send an answer, show the analyst thinking, then load the next question.
  var answerForm = document.getElementById("answer-form");
  if (answerForm) {
    var answerBox = document.getElementById("answer");
    var answerButton = document.getElementById("answer-button");
    var answerError = document.getElementById("answer-error");
    var thinking = inlineLoader(answerForm);
    var answerToken = answerForm.querySelector("input[name=csrfmiddlewaretoken]").value;
    var sending = false;
    answerForm.querySelectorAll(".example-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        answerBox.value = chip.getAttribute("data-answer") || chip.textContent.trim();
        answerBox.focus();
        answerBox.setSelectionRange(answerBox.value.length, answerBox.value.length);
      });
    });
    answerBox.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        answerForm.requestSubmit();
      }
    });
    var fail = function (message) {
      thinking.stop();
      answerForm.classList.remove("is-thinking");
      answerError.textContent = message;
      answerError.hidden = false;
      answerButton.disabled = false;
      answerButton.textContent = "Send answer";
      sending = false;
    };
    answerForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (sending) return;
      if (!answerBox.value.trim()) { fail("Write an answer, tap an example, or tap Skip."); return; }
      sending = true;
      answerError.hidden = true;
      answerButton.disabled = true;
      answerButton.textContent = "Sending";
      answerForm.classList.add("is-thinking");
      thinking.start();
      var body = new URLSearchParams();
      body.set("answer", answerBox.value);
      fetch(answerForm.getAttribute("data-url"), {
        method: "POST",
        headers: { "X-CSRFToken": answerToken, "X-Requested-With": "fetch" },
        credentials: "same-origin",
        body: body
      }).then(function (response) {
        return response.json().then(function (data) { return { ok: response.ok, data: data }; });
      }).then(function (result) {
        if (!result.ok && !result.data.saved) {
          fail(result.data.error || "The analyst did not answer. Press Send again.");
          return;
        }
        // Saved: show the next question, or, if the analyst failed, the page's retry button.
        window.location.replace(window.location.pathname + "?t=" + Date.now() + "#current");
      }).catch(function () {
        fail("The app did not answer. Your answer is still here; press Send again.");
      });
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

  // Stage 4: several workers each ask for one unit of work at a time (write a
  // piece's prompts, or make and review one image). The server claims each
  // unit before starting it, so workers never repeat each other's work.
  var visualMake = document.getElementById("visual-make");
  var visualButton = document.getElementById("visual-button");
  if (visualMake && visualButton) {
    var vToken = visualMake.querySelector("input[name=csrfmiddlewaretoken]").value;
    var vFill = document.getElementById("visual-fill");
    var vStatus = document.getElementById("visual-status");
    var vLast = document.getElementById("visual-last");
    var workers = Math.max(1, parseInt(visualMake.getAttribute("data-workers"), 10) || 1);
    var running = 0;
    var stopped = false;
    var stopMessage = "";
    var visualLoader = inlineLoader(visualMake);
    var vBar = visualMake.querySelector("[role=progressbar]");
    var showVisual = function (p) {
      if (!p) return;
      vFill.style.width = (p.shots ? Math.round(p.resolved / p.shots * 100) : 0) + "%";
      if (vBar) vBar.setAttribute("aria-valuenow", p.resolved);
      visualLoader.update(p.resolved, p.shots);
      visualLoader.set("spent", "$" + p.spent);
      vStatus.textContent = p.resolved + " of " + p.shots + " shots finished, " + p.made + " images made, " +
        p.ready + " piece" + (p.ready === 1 ? "" : "s") + " awaiting your decision. Spent $" + p.spent + " of $" + p.limit + ".";
    };
    var workerDone = function (message) {
      running -= 1;
      if (message) vLast.textContent = message;
      if (running > 0) return;
      if (stopped) {
        visualLoader.stop();
        vLast.textContent = stopMessage || "Stopped. Press Continue to carry on.";
        visualButton.disabled = false;
        visualButton.textContent = "Continue";
      } else {
        vLast.textContent = "All images are made and reviewed. Loading them.";
        window.location.reload();
      }
    };
    var work = function () {
      if (stopped) { workerDone(); return; }
      fetch(visualMake.getAttribute("data-url"), {
        method: "POST",
        headers: { "X-CSRFToken": vToken, "X-Requested-With": "fetch" },
        credentials: "same-origin"
      }).then(function (response) {
        return response.json().then(function (body) { return { ok: response.ok, body: body }; });
      }).then(function (result) {
        var body = result.body;
        showVisual(body.progress);
        if (!result.ok || body.state === "error") {
          stopped = true;
          stopMessage = body.error || stopMessage;
          workerDone();
          return;
        }
        if (body.state === "worked") {
          vLast.textContent = body.did;
          visualLoader.note(body.did, /failed|did not pass|gave up/i.test(body.did));
          work();
        } else if (body.state === "waiting") {
          if (body.did) vLast.textContent = body.did;
          window.setTimeout(work, 4000 + Math.floor(Math.random() * 3000));
        } else {
          workerDone();
        }
      }).catch(function () {
        stopped = true;
        stopMessage = stopMessage || "The app did not answer. Press Continue to carry on; nothing made so far is lost.";
        workerDone();
      });
    };
    visualButton.addEventListener("click", function () {
      visualButton.disabled = true;
      visualButton.textContent = "Making images";
      vLast.textContent = "Starting.";
      visualLoader.start();
      stopped = false;
      stopMessage = "";
      for (var i = 0; i < workers; i += 1) {
        running += 1;
        window.setTimeout(work, i * 700);
      }
    });
  }

  // Reviewer check: the founder's own accept or reject on stage 4 images.
  // Pressing the chosen button again clears it.
  var markGrid = document.querySelector(".mark-grid");
  if (markGrid) {
    var markToken = document.querySelector("#mark input[name=csrfmiddlewaretoken]").value;
    var markCount = document.getElementById("mark-count");
    markGrid.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-verdict]");
      if (!button) return;
      var item = button.closest(".mark-item");
      var wanted = button.getAttribute("aria-pressed") === "true" ? "" : button.getAttribute("data-verdict");
      var body = new FormData();
      body.append("verdict", wanted);
      fetch(item.getAttribute("data-url"), {
        method: "POST", body: body, credentials: "same-origin",
        headers: { "X-CSRFToken": markToken, "X-Requested-With": "fetch" }
      }).then(function (response) { return response.json(); }).then(function (data) {
        if (data.error) return;
        item.setAttribute("data-verdict", data.verdict);
        item.querySelectorAll("button[data-verdict]").forEach(function (b) {
          b.setAttribute("aria-pressed", String(b.getAttribute("data-verdict") === data.verdict));
        });
        if (markCount) markCount.textContent = "Marked so far: " + data.accept + " accept, " + data.reject + " reject.";
      });
    });
  }
})();
