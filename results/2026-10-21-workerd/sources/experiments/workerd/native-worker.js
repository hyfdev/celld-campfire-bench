function unexpected() {
  throw new Error("A JavaScript application callback ran in the direct Wasm mode");
}

export class ChatRoom {
  constructor() { unexpected(); }
  fetch() { unexpected(); }
  webSocketMessage() { unexpected(); }
  webSocketClose() { unexpected(); }
}

export default {fetch: unexpected};
