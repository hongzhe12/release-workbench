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

    var branchPicker = document.querySelector("[data-branch-picker]");
    if (branchPicker) {
        var pickerMenu = branchPicker.querySelector("[data-branch-menu]");
        var pickerSummary = branchPicker.querySelector("[data-branch-summary]");
        var pickerOptions = Array.from(
            branchPicker.querySelectorAll("[data-branch-option]")
        );
        var pickerBoxes = pickerOptions.map(function (option) {
            return option.querySelector("input");
        });
        var syncPickerSummary = function () {
            var count = pickerBoxes.filter(function (box) {
                return box.checked;
            }).length;
            pickerSummary.textContent = count ? "已选择 " + count + " 个分支" : "选择分支";
        };
        branchPicker.querySelector("[data-branch-toggle]").addEventListener(
            "click",
            function () {
                pickerMenu.hidden = !pickerMenu.hidden;
            }
        );
        branchPicker.querySelector("[data-branch-search]").addEventListener(
            "input",
            function (event) {
                var keyword = event.target.value.trim().toLowerCase();
                pickerOptions.forEach(function (option) {
                    option.hidden = !option.textContent
                        .toLowerCase()
                        .includes(keyword);
                });
            }
        );
        pickerBoxes.forEach(function (box) {
            box.addEventListener("change", syncPickerSummary);
        });
        syncPickerSummary();
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
