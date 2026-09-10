"use strict";

class BugAgentApi {
  constructor(baseUrl, apiKey) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
  }

  async request(path, options = {}) {
    const response = await fetch(this.baseUrl + path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(this.apiKey ? {"X-API-Key": this.apiKey} : {}),
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let message = String(response.status) + " " + response.statusText;
      try {
        const body = await response.json();
        message = body.detail || message;
      } catch {}
      throw new Error(message);
    }
    return response.status === 204 ? undefined : response.json();
  }

  health() { return this.request("/health"); }
  listConversations() { return this.request("/conversations"); }
  getConversation(id) {
    return this.request("/conversations/" + encodeURIComponent(id));
  }
  createConversation(payload) {
    return this.request("/conversations", {
      method: "POST", body: JSON.stringify(payload),
    });
  }
  sendMessage(id, content) {
    return this.request("/conversations/" + encodeURIComponent(id) + "/messages", {
      method: "POST", body: JSON.stringify({content}),
    });
  }
  cancelMessage(conversationId, messageId) {
    return this.request(
      "/conversations/" + encodeURIComponent(conversationId) +
      "/messages/" + String(messageId) + "/cancel",
      {method: "POST"},
    );
  }
  getEvents(id, after = 0) {
    return this.request(
      "/conversations/" + encodeURIComponent(id) + "/events?after=" + String(after),
    );
  }

  async streamEvents(id, after, signal, onEvent) {
    const url = this.baseUrl + "/conversations/" + encodeURIComponent(id) +
      "/events/stream?after=" + String(after);
    const response = await fetch(url, {
      headers: this.apiKey ? {"X-API-Key": this.apiKey} : {},
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error("事件流连接失败：" + String(response.status));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const next = await reader.read();
      if (next.done) break;
      buffer += decoder.decode(next.value, {stream: true});
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = block.split("\n").find((line) => line.startsWith("data: "));
        if (data) onEvent(JSON.parse(data.slice(6)));
        boundary = buffer.indexOf("\n\n");
      }
    }
  }
}

module.exports = {BugAgentApi};
