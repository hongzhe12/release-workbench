(function () {
    "use strict";

    function schedulePoll(element) {
        var url = element.dataset.pollUrl;
        if (!url || element.dataset.polling === "1") {
            return;
        }
        element.dataset.polling = "1";
        window.setTimeout(async function () {
            try {
                var response = await fetch(url, {
                    headers: { "X-Requested-With": "XMLHttpRequest" }
                });
                if (!response.ok) {
                    throw new Error("HTTP " + response.status);
                }
                var wrapper = document.createElement("div");
                wrapper.innerHTML = await response.text();
                var replacement = wrapper.firstElementChild;
                element.replaceWith(replacement);
                schedulePoll(replacement);
            } catch (error) {
                element.dataset.polling = "0";
                schedulePoll(element);
            }
        }, 1500);
    }

    document.querySelectorAll("[data-poll-url]").forEach(schedulePoll);

    document.querySelectorAll("[data-auto-submit]").forEach(function (select) {
        select.addEventListener("change", function () {
            select.form.submit();
        });
    });

    document.querySelectorAll("[data-confirm]").forEach(function (element) {
        element.addEventListener("click", function (event) {
            if (!window.confirm(element.dataset.confirm)) {
                event.preventDefault();
            }
        });
    });

    var branchModeInputs = document.querySelectorAll(
        '.branch-mode-options input[name="mode"]'
    );
    if (branchModeInputs.length) {
        var syncBranchMode = function () {
            var selected = document.querySelector(
                '.branch-mode-options input[name="mode"]:checked'
            );
            var mode = selected ? selected.value : "create";
            document.querySelectorAll("[data-branch-panel]").forEach(function (panel) {
                panel.hidden = panel.dataset.branchPanel !== mode;
            });
        };
        branchModeInputs.forEach(function (input) {
            input.addEventListener("change", syncBranchMode);
        });
        syncBranchMode();
    }

    document.querySelectorAll("form").forEach(function (form) {
        form.addEventListener("submit", function () {
            window.setTimeout(function () {
                form.querySelectorAll("button[type='submit']").forEach(function (button) {
                    button.disabled = true;
                    button.dataset.originalText = button.textContent;
                    button.textContent = "处理中...";
                });
            }, 0);
        });
    });
})();
