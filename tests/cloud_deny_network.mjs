// Fixture-only guard. Never imports an operating client or live credential.
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
const deny = () => { throw new Error('network disabled in synthetic validation'); };
globalThis.fetch = deny;
net.Socket.prototype.connect = deny;
net.connect = deny;
net.createConnection = deny;
http.request = deny;
http.get = deny;
https.request = deny;
https.get = deny;
