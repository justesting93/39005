const state = {
  tab: "members",
  fy: null,
  meta: null,
  eventId: null,
  trainingId: null,
  selectionReport: null,
  hoursReport: null,
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function fmtHours(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const rounded = Math.round(Number(value) * 100) / 100;
  return String(rounded);
}

function fyLabel(year) {
  return `${year}/${String((year + 1) % 100).padStart(2, "0")}`;
}

function prettyWhen(value) {
  return value ? value.replace("T", " ") : "";
}

function banner(text, kind = "") {
  const el = $("banner");
  el.hidden = !text;
  el.textContent = text || "";
  el.className = kind ? `banner ${kind}` : "banner";
}

function partySelect(selected, onChange) {
  const select = h("select", { "aria-label": "Duty party", onChange });
  for (const party of ["DP1", "DP2", "DP3"]) {
    select.append(h("option", { value: party, text: party, selected: party === selected }));
  }
  return select;
}

function fillYearSelect() {
  const select = $("fy-select");
  select.replaceChildren();
  for (const year of state.meta.fy_years) {
    select.append(h("option", { value: year, text: fyLabel(year), selected: year === state.fy }));
  }
}

async function init() {
  state.meta = await api("/api/meta");
  state.fy = state.meta.current_fy;
  fillYearSelect();
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  });
  $("fy-select").addEventListener("change", async () => {
    state.fy = Number($("fy-select").value);
    await refresh();
  });
  $("restore").addEventListener("click", restoreSample);
  $("print-selection").addEventListener("click", () => window.print());
  $("print-hours").addEventListener("click", () => window.print());
  $("csv-selection").addEventListener("click", downloadSelectionCsv);
  $("csv-hours").addEventListener("click", downloadHoursCsv);
  $("member-form").addEventListener("submit", addMember);
  $("event-form").addEventListener("submit", addEvent);
  $("training-form").addEventListener("submit", addTraining);
  banner("");
  await showTab("members");
}

async function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const on = button.dataset.tab === tab;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tab;
  });
  $("fy-control").hidden = !(tab === "members" || tab === "hours");
  await refresh();
}

async function refresh() {
  try {
    if (state.tab === "members") await renderMembers();
    else if (state.tab === "events") await renderEvents();
    else if (state.tab === "training") await renderTraining();
    else if (state.tab === "assign") await renderAssign();
    else if (state.tab === "selection") await renderSelection();
    else if (state.tab === "hours") await renderHours();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function renderMembers() {
  const data = await api(`/api/members?fy=${state.fy}`);
  const body = $("member-rows");
  body.replaceChildren();
  if (!data.members.length) {
    body.append(h("tr", {}, h("td", { colspan: "9", text: "No members yet." })));
    return;
  }
  for (const member of data.members) {
    const idInput = h("input", {
      value: member.member_id,
      "aria-label": `Member ID ${member.member_id}`,
      maxlength: "32",
      onChange: async (event) => {
        await saveMember(member.id, { member_id: event.target.value.trim() });
      },
    });
    const orderInput = h("input", {
      type: "number",
      min: "1",
      value: member.queue_order,
      "aria-label": `Queue order for ${member.member_id}`,
      onChange: async (event) => {
        const order = Number(event.target.value);
        if (!Number.isInteger(order) || order < 1) {
          banner("Queue order must be a whole number of at least 1.", "error");
          await renderMembers();
          return;
        }
        await saveMember(member.id, { queue_order: order });
      },
    });
    body.append(
      h("tr", {}, [
        h("td", {}, idInput),
        h("td", {}, partySelect(member.party, (event) => saveMember(member.id, { party: event.target.value }))),
        h("td", {}, orderInput),
        h("td", { text: fmtHours(member.attend_hours) }),
        h("td", { text: `${fmtHours(member.training_hours)} / ${fmtHours(member.training_offered)}` }),
        h("td", { text: fmtHours(member.duty_hours) }),
        h("td", { text: String(member.duty_count) }),
        h("td", {}, h("span", { class: member.meets_requirement ? "status met" : "status", text: member.status })),
        h("td", {}, h("div", { class: "actions" }, [
          h("button", { type: "button", class: "secondary", text: "Up", "aria-label": `Move ${member.member_id} up`, onClick: () => moveMember(member.id, "up") }),
          h("button", { type: "button", class: "secondary", text: "Down", "aria-label": `Move ${member.member_id} down`, onClick: () => moveMember(member.id, "down") }),
          h("button", { type: "button", class: "danger", text: "Delete", onClick: () => removeMember(member) }),
        ])),
      ]),
    );
  }
}

async function saveMember(id, payload) {
  try {
    await api(`/api/members/${id}?fy=${state.fy}`, { method: "PUT", body: JSON.stringify(payload) });
    banner("");
    await renderMembers();
  } catch (error) {
    banner(error.message, "error");
    await renderMembers();
  }
}

async function moveMember(id, direction) {
  try {
    await api(`/api/members/${id}/move?fy=${state.fy}`, {
      method: "POST",
      body: JSON.stringify({ direction }),
    });
    await renderMembers();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function removeMember(member) {
  if (!confirm(`Delete member ${member.member_id}? Applications and assignments for this member will be removed.`)) return;
  try {
    await api(`/api/members/${member.id}`, { method: "DELETE" });
    banner(`${member.member_id} deleted.`, "ok");
    await renderMembers();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function addMember(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api(`/api/members?fy=${state.fy}`, {
      method: "POST",
      body: JSON.stringify({ member_id: form.get("member_id"), party: form.get("party") }),
    });
    event.target.reset();
    banner("Member added.", "ok");
    await renderMembers();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function renderEvents() {
  const data = await api("/api/events");
  const body = $("event-rows");
  body.replaceChildren();
  if (!data.events.length) {
    body.append(h("tr", {}, h("td", { colspan: "8", text: "No duties yet." })));
    return;
  }
  for (const item of data.events) {
    body.append(h("tr", {}, [
      h("td", {}, h("input", {
        value: item.duty_code,
        "aria-label": `Duty code ${item.duty_code}`,
        onChange: (event) => saveEvent(item, { duty_code: event.target.value.trim() }),
      })),
      h("td", {}, h("input", {
        type: "datetime-local",
        value: item.start_datetime,
        "aria-label": `Duty start ${item.duty_code}`,
        onChange: (event) => saveEvent(item, { start_datetime: event.target.value }),
      })),
      h("td", {}, h("input", {
        type: "datetime-local",
        value: item.end_datetime,
        "aria-label": `Duty end ${item.duty_code}`,
        onChange: (event) => saveEvent(item, { end_datetime: event.target.value }),
      })),
      h("td", { text: fmtHours(item.hours) }),
      h("td", {}, h("input", {
        type: "number",
        min: "1",
        value: item.required_members,
        "aria-label": `Required members for ${item.duty_code}`,
        onChange: (event) => {
          const required = Number(event.target.value);
          if (!Number.isInteger(required) || required < 1) {
            banner("Required number of members must be a whole number of at least 1.", "error");
            renderEvents();
            return;
          }
          saveEvent(item, { required_members: required });
        },
      })),
      h("td", { text: String(item.applicant_count) }),
      h("td", {}, [
        document.createTextNode(item.assigned_member_ids.length ? item.assigned_member_ids.join(", ") : "—"),
        item.conflict ? h("div", { class: "detail", text: "Overlaps another duty" }) : null,
        h("div", { class: "detail", text: methodLabel(item.assignment_method) }),
      ]),
      h("td", {}, h("div", { class: "actions" }, [
        h("button", { type: "button", class: "secondary", text: "Open", onClick: () => openAssign(item.id) }),
        h("button", { type: "button", class: "danger", text: "Delete", onClick: () => removeEvent(item) }),
      ])),
    ]));
  }
}

function methodLabel(method) {
  if (method === "standard") return "Standard";
  if (method === "assign") return "Assign";
  return "Unassigned";
}

async function saveEvent(item, payload) {
  try {
    await api(`/api/events/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
    banner("");
    await renderEvents();
  } catch (error) {
    banner(error.message, "error");
    await renderEvents();
  }
}

async function removeEvent(item) {
  if (!confirm(`Delete duty ${item.duty_code}?`)) return;
  try {
    await api(`/api/events/${item.id}`, { method: "DELETE" });
    if (state.eventId === item.id) state.eventId = null;
    banner(`${item.duty_code} deleted.`, "ok");
    await renderEvents();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function addEvent(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const required = Number(form.get("required_members"));
  try {
    await api("/api/events", {
      method: "POST",
      body: JSON.stringify({
        duty_code: form.get("duty_code"),
        start_datetime: form.get("start_datetime"),
        end_datetime: form.get("end_datetime"),
        required_members: required,
      }),
    });
    event.target.reset();
    banner("Event added.", "ok");
    await renderEvents();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function renderTraining() {
  const [trainingData, memberData] = await Promise.all([
    api("/api/trainings"),
    api(`/api/members?fy=${state.fy}`),
  ]);
  const body = $("training-rows");
  body.replaceChildren();
  if (!trainingData.trainings.length) {
    body.append(h("tr", {}, h("td", { colspan: "6", text: "No training sessions yet." })));
    $("attendance-panel").replaceChildren();
    return;
  }
  if (!state.trainingId || !trainingData.trainings.some((item) => item.id === state.trainingId)) {
    state.trainingId = trainingData.trainings[0].id;
  }
  for (const item of trainingData.trainings) {
    body.append(h("tr", {}, [
      h("td", {}, h("input", {
        value: item.training_code,
        "aria-label": `Training code ${item.training_code}`,
        onChange: (event) => saveTraining(item, { training_code: event.target.value.trim() }),
      })),
      h("td", {}, h("input", {
        type: "datetime-local",
        value: item.start_datetime,
        "aria-label": `Training start ${item.training_code}`,
        onChange: (event) => saveTraining(item, { start_datetime: event.target.value }),
      })),
      h("td", {}, h("input", {
        type: "datetime-local",
        value: item.end_datetime,
        "aria-label": `Training end ${item.training_code}`,
        onChange: (event) => saveTraining(item, { end_datetime: event.target.value }),
      })),
      h("td", { text: fmtHours(item.hours) }),
      h("td", { text: String(item.attendee_ids.length) }),
      h("td", {}, h("div", { class: "actions" }, [
        h("button", {
          type: "button",
          class: item.id === state.trainingId ? "" : "secondary",
          text: "Attendance",
          onClick: async () => {
            state.trainingId = item.id;
            await renderTraining();
          },
        }),
        h("button", { type: "button", class: "danger", text: "Delete", onClick: () => removeTraining(item) }),
      ])),
    ]));
  }
  const selected = trainingData.trainings.find((item) => item.id === state.trainingId);
  paintAttendance(selected, memberData.members);
}

function paintAttendance(training, members) {
  const panel = $("attendance-panel");
  if (!training) {
    panel.replaceChildren();
    return;
  }
  const attended = new Set(training.attendee_ids);
  const grid = h("div", { class: "table-wrap" }, h("table", {}, [
    h("thead", {}, h("tr", {}, [
      h("th", { text: "Attended" }),
      h("th", { text: "Member ID" }),
      h("th", { text: "Duty party" }),
    ])),
    h("tbody", {}, members.map((member) => h("tr", {}, [
      h("td", {}, h("input", {
        type: "checkbox",
        checked: attended.has(member.member_id),
        "aria-label": `${member.member_id} attended ${training.training_code}`,
        onChange: (event) => setAttendance(training, member.member_id, event.target.checked),
      })),
      h("td", { text: member.member_id }),
      h("td", { text: member.party }),
    ]))),
  ]));
  panel.replaceChildren(
    h("h3", { text: `Attendance for ${training.training_code}` }),
    h("div", { class: "button-row no-print" }, [
      h("button", { type: "button", class: "secondary", text: "Mark all", onClick: () => setAttendanceList(training, members.map((member) => member.member_id)) }),
      h("button", { type: "button", class: "secondary", text: "Clear all", onClick: () => setAttendanceList(training, []) }),
    ]),
    grid,
  );
}

async function setAttendance(training, memberId, on) {
  const ids = new Set(training.attendee_ids);
  if (on) ids.add(memberId);
  else ids.delete(memberId);
  await setAttendanceList(training, [...ids]);
}

async function setAttendanceList(training, memberIds) {
  try {
    await api(`/api/trainings/${training.id}/attendees`, {
      method: "PUT",
      body: JSON.stringify({ member_ids: memberIds }),
    });
    banner("");
    await renderTraining();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function saveTraining(item, payload) {
  try {
    await api(`/api/trainings/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
    banner("");
    await renderTraining();
  } catch (error) {
    banner(error.message, "error");
    await renderTraining();
  }
}

async function removeTraining(item) {
  if (!confirm(`Delete training ${item.training_code}?`)) return;
  try {
    await api(`/api/trainings/${item.id}`, { method: "DELETE" });
    if (state.trainingId === item.id) state.trainingId = null;
    banner(`${item.training_code} deleted.`, "ok");
    await renderTraining();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function addTraining(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    const created = await api("/api/trainings", {
      method: "POST",
      body: JSON.stringify({
        training_code: form.get("training_code"),
        start_datetime: form.get("start_datetime"),
        end_datetime: form.get("end_datetime"),
      }),
    });
    state.trainingId = created.id;
    event.target.reset();
    banner("Training added.", "ok");
    await renderTraining();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function openAssign(eventId) {
  state.eventId = eventId;
  await showTab("assign");
}

async function renderAssign() {
  const data = await api("/api/events");
  const host = $("assign-body");
  if (!data.events.length) {
    host.replaceChildren(h("p", { text: "Add a duty on the Events tab first." }));
    return;
  }
  if (!state.eventId || !data.events.some((item) => item.id === state.eventId)) {
    const open = data.events.find((item) => !item.assignment_method) || data.events[0];
    state.eventId = open.id;
  }
  const workspace = await api(`/api/events/${state.eventId}/workspace`);
  paintAssign(data.events, workspace);
}

function paintAssign(events, workspace) {
  const select = h("select", {
    "aria-label": "Duty to assign",
    onChange: (event) => {
      state.eventId = Number(event.target.value);
      renderAssign();
    },
  });
  for (const item of events) {
    const assigned = item.assigned_member_ids.length ? ` · ${item.assigned_member_ids.join(", ")}` : "";
    select.append(h("option", {
      value: item.id,
      selected: item.id === workspace.event.id,
      text: `${item.duty_code} · ${prettyWhen(item.start_datetime)} · needs ${item.required_members}${assigned}`,
    }));
  }
  const standardMap = new Map(workspace.previews.standard.rows.map((row) => [row.member_id, row]));
  const assignMap = new Map(workspace.previews.assign.rows.map((row) => [row.member_id, row]));
  const standardPicks = new Set(workspace.previews.standard.selected);
  const assignPicks = new Set(workspace.previews.assign.selected);
  const table = h("table", {}, [
    h("thead", {}, h("tr", {}, [
      "Applied", "Member ID", "Duty party", "Attends hours", "Training", "Duties", "Standard rank", "Standard", "Assign",
    ].map((label) => h("th", { text: label })))),
    h("tbody", {}, workspace.members.map((member) => {
      const classes = [
        standardPicks.has(member.member_id) ? "pick-standard" : "",
        assignPicks.has(member.member_id) ? "pick-assign" : "",
      ].filter(Boolean).join(" ");
      return h("tr", { class: classes }, [
        h("td", {}, h("input", {
          type: "checkbox",
          checked: member.applied,
          "aria-label": `${member.member_id} applied`,
          onChange: (event) => setApplied(workspace, member.member_id, event.target.checked),
        })),
        h("td", { text: member.member_id }),
        h("td", { text: member.party }),
        h("td", { text: fmtHours(member.attend_hours) }),
        h("td", { text: `${fmtHours(member.training_hours)} / ${fmtHours(member.training_offered)}` }),
        h("td", { text: String(member.duty_count) }),
        h("td", { text: String(member.standard_rank) }),
        h("td", { text: standardMap.get(member.member_id)?.outcome || "" }),
        h("td", { text: assignMap.get(member.member_id)?.outcome || "" }),
      ]);
    })),
  ]);
  const host = $("assign-body");
  const pieces = [
    h("div", { class: "inline-form" }, [
      h("label", {}, ["Duty", select]),
    ]),
    h("p", { class: "muted", text: `Hour rules use financial year ${workspace.fy.label} (${workspace.fy.start} to ${workspace.fy.end}) because this duty starts then. Each earlier duty moves the party turn on by one, whether or not anyone was assigned. Member queues move only for people who were assigned.` }),
  ];
  if (workspace.earlier.length) {
    pieces.push(h("p", { text: `Earlier duties in the rolling list: ${workspace.earlier.map((item) => `${item.duty_code} (${item.member_ids.length ? item.member_ids.join(", ") : "party turn only"})`).join("; ")}.` }));
  }
  if (workspace.later.length) {
    pieces.push(h("p", { class: "callout warn", text: `Already assigned later, so not part of this rolling position: ${workspace.later.map((item) => item.duty_code).join(", ")}. Re-run if this duty should flow forward.` }));
  }
  pieces.push(renderRolling(workspace));
  pieces.push(h("div", { class: "button-row" }, [
    h("button", { type: "button", class: "secondary", text: "Mark all applied", onClick: () => setAppliedList(workspace, workspace.members.map((member) => member.member_id)) }),
    h("button", { type: "button", class: "secondary", text: "Clear applied", onClick: () => setAppliedList(workspace, []) }),
    ...["DP1", "DP2", "DP3"].map((party) => h("button", {
      type: "button",
      class: "secondary",
      text: `${party} on`,
      onClick: () => setPartyApplied(workspace, party, true),
    })),
  ]));
  pieces.push(h("p", { class: "muted", text: "Green bar: Standard would select this member. Tinted row: Assign would select this member." }));
  pieces.push(h("div", { class: "table-wrap" }, table));
  pieces.push(h("div", { class: "reason-columns" }, [
    reasonCard("Standard preview", workspace.previews.standard),
    reasonCard("Assign preview", workspace.previews.assign),
  ]));
  pieces.push(h("div", { class: "button-row" }, [
    h("button", { type: "button", id: "run-standard", text: "Standard", onClick: () => runMethod("standard") }),
    h("button", { type: "button", id: "run-assign", class: "seal", text: "Assign", onClick: () => runMethod("assign") }),
    h("button", {
      type: "button",
      class: "danger",
      text: "Clear assignment",
      disabled: !workspace.saved,
      onClick: () => clearSaved(workspace),
    }),
    h("button", {
      type: "button",
      class: "secondary",
      text: "Re-run this and later",
      disabled: !workspace.saved,
      onClick: () => rerun(workspace),
    }),
  ]));
  if (workspace.saved) {
    const savedBits = [
      h("h3", { text: `Saved ${methodLabel(workspace.saved.method)} decision` }),
      h("p", { text: workspace.saved.summary }),
      h("p", { text: workspace.saved.rolling_note }),
    ];
    if (workspace.saved_stale) {
      savedBits.push(h("p", { class: "callout warn", text: workspace.saved_stale_reasons.join(" ") }));
    }
    savedBits.push(h("ol", {}, (workspace.saved.rows || []).filter((row) => row.selected).map((row) => (
      h("li", {}, [h("strong", { text: row.member_id }), ` — ${row.reason}`])
    ))));
    pieces.push(h("div", { class: "report-block" }, savedBits));
  }
  host.replaceChildren(...pieces);
}

function renderRolling(workspace) {
  const applied = new Set(workspace.members.filter((member) => member.applied).map((member) => member.member_id));
  return h("div", { class: "queues" }, workspace.rolling.party_order.map((party, index) => (
    h("div", { class: "queue-line" }, [
      h("span", { class: index === 0 ? "party-tag first" : "party-tag", text: index === 0 ? `${party} first` : party }),
      ...(workspace.rolling.queues[party] || []).map((memberId) => h("span", {
        class: applied.has(memberId) ? "chip" : "chip out",
        text: memberId,
        title: applied.has(memberId) ? "Applied" : "Has not applied",
      })),
    ])
  )));
}

function reasonCard(title, plan) {
  const names = plan.selected.length ? plan.selected.join(", ") : "nobody";
  const gap = plan.shortfall ? ` Short by ${plan.shortfall}.` : "";
  return h("section", { class: "reason-card" }, [
    h("h3", { text: title }),
    h("p", {}, [h("strong", { text: names }), gap]),
    h("p", { class: "muted", text: "Preview only, until Standard or Assign is clicked." }),
    h("ol", {}, plan.rows.map((row) => h("li", {}, [h("strong", { text: `${row.member_id} · ${row.outcome}` }), ` ${row.reason}`]))),
  ]);
}

async function setApplied(workspace, memberId, on) {
  const ids = new Set(workspace.members.filter((member) => member.applied).map((member) => member.member_id));
  if (on) ids.add(memberId);
  else ids.delete(memberId);
  await setAppliedList(workspace, [...ids]);
}

async function setPartyApplied(workspace, party, on) {
  const ids = new Set(workspace.members.filter((member) => member.applied).map((member) => member.member_id));
  for (const member of workspace.members.filter((item) => item.party === party)) {
    if (on) ids.add(member.member_id);
    else ids.delete(member.member_id);
  }
  await setAppliedList(workspace, [...ids]);
}

async function setAppliedList(workspace, memberIds) {
  try {
    await api(`/api/events/${workspace.event.id}/applications`, {
      method: "PUT",
      body: JSON.stringify({ member_ids: memberIds }),
    });
    banner("");
    await renderAssign();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function runMethod(method) {
  try {
    const result = await api(`/api/events/${state.eventId}/assignment`, {
      method: "POST",
      body: JSON.stringify({ method }),
    });
    const names = result.plan.selected.length ? result.plan.selected.join(", ") : "nobody";
    banner(`${result.plan.duty_code}: ${methodLabel(method)} selected ${names}.`, "ok");
    await renderAssign();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function clearSaved(workspace) {
  if (!confirm(`Clear the saved assignment for ${workspace.event.duty_code}?`)) return;
  try {
    await api(`/api/events/${workspace.event.id}/assignment`, { method: "DELETE" });
    banner(`Assignment cleared for ${workspace.event.duty_code}.`, "ok");
    await renderAssign();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function rerun(workspace) {
  if (!confirm("Re-apply the saved method on this duty and every later duty that is already assigned, in date order.")) return;
  try {
    const result = await api(`/api/events/${workspace.event.id}/rerun`, { method: "POST", body: "{}" });
    const text = result.results.map((item) => `${item.duty_code}: ${item.selected.join(", ") || "nobody"}`).join(" · ");
    banner(`Re-ran ${text}.`, "ok");
    await renderAssign();
  } catch (error) {
    banner(error.message, "error");
  }
}

async function renderSelection() {
  const data = await api("/api/reports/selection");
  state.selectionReport = data;
  const host = $("selection-report");
  if (!data.events.length) {
    host.replaceChildren(h("p", { text: "No duties yet." }));
    return;
  }
  host.replaceChildren(...data.events.map((item) => {
    const event = item.event;
    const bits = [
      h("h3", { text: `${event.duty_code} · ${prettyWhen(event.start_datetime)} to ${prettyWhen(event.end_datetime)}` }),
      h("p", { class: "muted", text: `${fmtHours(event.hours)} hours · needs ${event.required_members}` }),
    ];
    if (!item.decided) {
      bits.push(h("p", { text: "Not assigned yet. Open Assign and run Standard or Assign." }));
      return h("article", { class: "report-block" }, bits);
    }
    const log = item.log;
    bits.push(h("p", { text: log.summary }));
    bits.push(h("p", { text: log.rolling_note }));
    if (log.financial_year) {
      bits.push(h("p", { class: "muted", text: `Decided ${log.generated_at} for financial year ${log.financial_year.label}.` }));
    }
    if (item.stale.stale) {
      bits.push(h("p", { class: "callout warn", text: item.stale.reasons.join(" ") }));
    }
    bits.push(selectionTable(log.rows || []));
    return h("article", { class: "report-block" }, bits);
  }));
}

function selectionTable(rows) {
  return h("div", { class: "table-wrap" }, h("table", {}, [
    h("thead", {}, h("tr", {}, [
      "Member ID", "Outcome", "Attends hours", "Training", "Duties", "Standard rank", "Why",
    ].map((label) => h("th", { text: label })))),
    h("tbody", {}, rows.map((row) => h("tr", {}, [
      h("td", { text: row.member_id }),
      h("td", { text: row.outcome }),
      h("td", { text: fmtHours(row.attend_hours) }),
      h("td", { text: `${fmtHours(row.training_hours)} / ${fmtHours(row.training_offered)}` }),
      h("td", { text: String(row.duty_count) }),
      h("td", { text: String(row.standard_rank) }),
      h("td", { text: row.reason }),
    ]))),
  ]));
}

async function renderHours() {
  const data = await api(`/api/reports/hours?fy=${state.fy}`);
  state.hoursReport = data;
  const offered = data.training_offered;
  const rule = offered
    ? `Training offered is ${fmtHours(offered)} hours, so 30% is ${fmtHours(data.training_target)} hours.`
    : "No training is offered in this financial year, so the 30% training rule is treated as met.";
  const host = $("hours-report");
  host.replaceChildren(
    h("div", { class: "stats" }, [
      h("div", { class: "stat" }, [h("b", { text: fyLabel(data.fy.year) }), "1 Apr – 31 Mar"]),
      h("div", { class: "stat" }, [h("b", { text: String(data.met_count) }), "Met both rules"]),
      h("div", { class: "stat" }, [h("b", { text: String(data.short_count) }), "Still short"]),
      h("div", { class: "stat" }, [h("b", { text: fmtHours(offered) }), "Training hours offered"]),
    ]),
    h("p", { text: `Each member needs ${fmtHours(data.hour_target)} hours of training plus duty. ${rule} Generated ${prettyWhen(data.generated_at)}.` }),
    h("div", { class: "table-wrap" }, h("table", {}, [
      h("thead", {}, h("tr", {}, [
        "Member ID", "Duty party", "Training hours", "Duty hours", "Attends hours", "Short of 60", "Training %", "Duties", "Requirement",
      ].map((label) => h("th", { text: label })))),
      h("tbody", {}, data.rows.flatMap((row) => {
        const duties = row.duties.map((item) => `${item.duty_code} ${fmtHours(item.hours)}h`).join(", ") || "No duties";
        const trainings = row.trainings.map((item) => `${item.training_code} ${fmtHours(item.hours)}h`).join(", ") || "No training";
        return [
          h("tr", {}, [
            h("td", { text: row.member_id }),
            h("td", { text: row.party }),
            h("td", { text: fmtHours(row.training_hours) }),
            h("td", { text: fmtHours(row.duty_hours) }),
            h("td", { text: fmtHours(row.attend_hours) }),
            h("td", { text: fmtHours(row.hour_shortfall) }),
            h("td", { text: row.training_percent == null ? "—" : `${row.training_percent}%` }),
            h("td", { text: String(row.duty_count) }),
            h("td", {}, h("span", { class: row.meets_requirement ? "status met" : "status", text: row.status })),
          ]),
          h("tr", {}, h("td", { class: "detail", colspan: "9", text: `Duties: ${duties}. Training: ${trainings}.` })),
        ];
      })),
    ])),
  );
}

function download(filename, rows) {
  const lines = rows.map((row) => row.map(csvCell).join(",")).join("\r\n");
  const blob = new Blob([lines], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = h("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  if (/[",\r\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
  return text;
}

function downloadSelectionCsv() {
  const data = state.selectionReport;
  if (!data) return;
  const rows = [["Duty code", "Method", "Member ID", "Outcome", "Attends hours", "Duty count", "Standard rank", "Why"]];
  for (const item of data.events) {
    if (!item.decided) continue;
    for (const row of item.log.rows || []) {
      rows.push([
        item.event.duty_code,
        item.log.method,
        row.member_id,
        row.outcome,
        row.attend_hours,
        row.duty_count,
        row.standard_rank,
        row.reason,
      ]);
    }
  }
  download("selection-report.csv", rows);
}

function downloadHoursCsv() {
  const data = state.hoursReport;
  if (!data) return;
  const rows = [["Financial year", "Member ID", "Duty party", "Training hours", "Duty hours", "Attends hours", "Short of 60", "Training %", "Duty count", "Status", "Duties", "Training"]];
  for (const row of data.rows) {
    rows.push([
      data.fy.label,
      row.member_id,
      row.party,
      row.training_hours,
      row.duty_hours,
      row.attend_hours,
      row.hour_shortfall,
      row.training_percent ?? "",
      row.duty_count,
      row.status,
      row.duties.map((item) => item.duty_code).join(" "),
      row.trainings.map((item) => item.training_code).join(" "),
    ]);
  }
  download(`hours-${data.fy.label.replace("/", "-")}.csv`, rows);
}

async function restoreSample() {
  if (!confirm("Replace the current roster with the 12-member sample?")) return;
  try {
    await api("/api/demo/restore", { method: "POST", body: "{}" });
    state.eventId = null;
    state.trainingId = null;
    state.meta = await api("/api/meta");
    state.fy = state.meta.current_fy;
    fillYearSelect();
    banner("Sample roster restored.", "ok");
    await refresh();
  } catch (error) {
    banner(error.message, "error");
  }
}

init().catch((error) => banner(error.message, "error"));
