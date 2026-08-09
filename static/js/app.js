document.addEventListener("DOMContentLoaded", function () {
    initializePage();
});

document.body.addEventListener("htmx:afterSwap", function () {
    initializePage();
});

document.body.addEventListener("htmx:responseError", function (event) {
    var message = "Cannot handle the request.";

    if (event.detail.xhr && event.detail.xhr.responseText) {
        message = event.detail.xhr.responseText;
    }

    showToast(message, true);
});

document.body.addEventListener("htmx:sendError", function () {
    showToast("Cannot connect to server.", true);
});

function initializePage() {
    focusFirstInvalidField();
    closeOpenDeckEditors();
}

function focusFirstInvalidField() {
    var invalidField = document.querySelector("input:invalid, select:invalid, textarea:invalid");

    if (!invalidField) return;
    if (document.activeElement && document.activeElement !== document.body) return;

    invalidField.focus();
}

function closeOpenDeckEditors() {
    var detailsElements = document.querySelectorAll(".deck-edit-panel[open]");

    detailsElements.forEach(function (details) {
        details.addEventListener("toggle", function () {
            if (!details.open) return;

            detailsElements.forEach(function (other) {
                if (other !== details) other.removeAttribute("open");
            });

            var input = details.querySelector("input");

            if (input) {
                window.requestAnimationFrame(function () {
                    input.focus();
                    input.select();
                });
            }
        }, { once: true });
    });
}

function showToast(message, isError) {
    var container = document.getElementById("toast-container");

    if (!container) return;

    var toast = document.createElement("div");

    toast.className = isError ? "alert alert-error toast" : "alert alert-success toast";
    toast.textContent = normalizeMessage(message);

    container.replaceChildren(toast);

    window.setTimeout(function () {
        toast.remove();
    }, 3500);
}

function normalizeMessage(message) {
    var temporary = document.createElement("div");

    temporary.innerHTML = String(message);

    return temporary.textContent.trim() || "Fail to handle request.";
}