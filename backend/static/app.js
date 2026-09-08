"use strict";
const demo = document.body.dataset.demo === "true";
const basePath = document.body.dataset.basePath || "";
const storageKey = "radar-demo-v1";
let saved = {};
try {
  saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
} catch {
  saved = {};
}
function persist() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(saved));
  } catch {
    toast("Browser storage is unavailable.");
  }
}
function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("visible");
  setTimeout(() => node.classList.remove("visible"), 2600);
}
function statusFor(id, fallback) {
  return saved[id]?.status || fallback || "new";
}
document.querySelectorAll("[data-save]").forEach((button) => {
  const id = button.dataset.save;
  const card = button.closest("[data-posting]");
  function render() {
    const state = demo
      ? statusFor(id, card.dataset.status)
      : card.dataset.status;
    const selected = state !== "new";
    button.textContent = selected ? "♥" : "♡";
    button.setAttribute("aria-pressed", String(selected));
  }
  render();
  button.addEventListener("click", async () => {
    const current = demo
      ? statusFor(id, card.dataset.status)
      : card.dataset.status;
    if (!["new", "interested"].includes(current)) {
      toast("Already tracked. Open the role to change its stage.");
      return;
    }
    const status = current === "new" ? "interested" : "new";
    if (demo) {
      saved[id] = { ...saved[id], status };
      persist();
    } else {
      const response = await fetch(`${basePath}/jobs/${id}/status`, {
        method: "POST",
        body: new URLSearchParams({ status }),
      });
      if (!response.ok) {
        toast("Could not save. Please try again.");
        return;
      }
      card.dataset.status = status;
    }
    render();
    toast(
      status === "interested"
        ? "Saved to your applications"
        : "Removed from saved opportunities",
    );
  });
});
document.querySelectorAll("[data-status-form]").forEach((form) => {
  const id = form.dataset.statusForm;
  if (demo && saved[id]) {
    form.elements.status.value = saved[id].status;
    form.elements.notes.value = saved[id].notes || "";
  }
  if (demo)
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saved[id] = {
        status: form.elements.status.value,
        notes: form.elements.notes.value,
      };
      persist();
      toast("Progress saved on this device");
    });
});
function renderApplications(stage = "all") {
  let count = 0;
  document.querySelectorAll("[data-application]").forEach((row) => {
    const status = demo
      ? statusFor(row.dataset.application, row.dataset.status)
      : row.dataset.status;
    const visible = status !== "new" && (stage === "all" || status === stage);
    row.hidden = !visible;
    row.querySelector(".stage-pill").textContent =
      status[0].toUpperCase() + status.slice(1);
    if (visible) count++;
  });
  const empty = document.querySelector("#application-empty");
  if (empty) empty.hidden = count > 0;
}
renderApplications();
document.querySelectorAll("[data-stage]").forEach((button) =>
  button.addEventListener("click", () => {
    document
      .querySelectorAll("[data-stage]")
      .forEach((b) => b.classList.toggle("active", b === button));
    renderApplications(button.dataset.stage);
  }),
);
document
  .querySelectorAll(".discovery-controls select, .chip-check input")
  .forEach((input) =>
    input.addEventListener("change", () => input.form.requestSubmit()),
  );
if (demo)
  document
    .querySelectorAll('form[method="post"]:not([data-status-form])')
    .forEach((form) =>
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        toast("This setting is available in the private workspace.");
      }),
    );
if ("serviceWorker" in navigator) {
  if (demo) {
    navigator.serviceWorker.register(`${basePath}/sw.js`).catch(() => {});
    navigator.serviceWorker.addEventListener("message", (event) => {
      if (event.data === "radar-offline-ready")
        toast("Demo saved for offline use");
    });
  } else {
    navigator.serviceWorker.getRegistrations().then((registrations) =>
      registrations.forEach((r) => {
        if (
          r.scope === new URL("/", location.href).href &&
          r.active?.scriptURL.endsWith("/sw.js")
        )
          r.unregister();
      }),
    );
  }
}
