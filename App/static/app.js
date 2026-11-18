// Minimal frontend: talks to the JSON API. Text is always inserted with
// textContent (never innerHTML) so task titles cannot inject markup.
const list = document.getElementById("task-list");
const form = document.getElementById("task-form");
const input = document.getElementById("title");
const message = document.getElementById("message");
const summary = document.getElementById("summary");

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || "Request failed (" + response.status + ")");
  }
  return response.status === 204 ? null : response.json();
}

function button(label, className, handler) {
  const el = document.createElement("button");
  el.textContent = label;
  el.className = className;
  el.addEventListener("click", handler);
  return el;
}

function render(tasks) {
  list.replaceChildren();
  tasks.forEach((task) => {
    const item = document.createElement("li");
    if (task.done) item.className = "done";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = task.title;
    item.append(
      title,
      button(task.done ? "Reopen" : "Done", "secondary", () =>
        run(() => api("/api/tasks/" + task.id, {
          method: "PUT",
          body: JSON.stringify({ done: !task.done }),
        }))),
      button("Delete", "danger", () =>
        run(() => api("/api/tasks/" + task.id, { method: "DELETE" })))
    );
    list.append(item);
  });
  const done = tasks.filter((t) => t.done).length;
  summary.textContent = tasks.length + " tasks, " + done + " done";
}

async function refresh() {
  const data = await api("/api/tasks");
  render(data.tasks);
}

async function run(action) {
  message.textContent = "";
  try {
    await action();
    await refresh();
  } catch (err) {
    message.textContent = err.message;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    await api("/api/tasks", { method: "POST", body: JSON.stringify({ title: input.value }) });
    input.value = "";
  });
});

run(refresh);
