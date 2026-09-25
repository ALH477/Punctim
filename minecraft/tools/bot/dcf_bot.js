#!/usr/bin/env node
// SPDX-License-Identifier: LGPL-3.0-only
// A Mineflayer bot as a console: joins a LAN world / server as an operator player and runs
// the commands `punctim mc --bot` hands it, returning the first feedback line that matches.
//
//   npm install            (in minecraft/tools/bot; Mineflayer is not vendored)
//   punctim mc --bot "node|minecraft/tools/bot/dcf_bot.js|--host|127.0.0.1|--port|PORT|--user|dcfbot" --peer ...
//
// Protocol on stdin/stdout, one JSON object per line:
//   in : {"cmd": "scoreboard players get w0 dcf_reg", "reply": "^w0 has", "timeout": 3}
//   out: {"reply": "w0 has 13832978 [DCF register]"}        (or {"reply": ""} when none/expired)
//   out: {"chat": "..."} for every other chat/system line (a chat-egress source without a log)
// The bot must be op'd (`/op dcfbot`) or in a cheats-on LAN world; it never moves.
'use strict'
const readline = require('readline')
const mineflayer = require('mineflayer')

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, arr) => {
  if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1]])
  return acc
}, []))
const bot = mineflayer.createBot({
  host: args.host || '127.0.0.1',
  port: parseInt(args.port || '25565', 10),
  username: args.user || 'dcfbot',
  auth: args.auth || 'offline',
  version: args.version || false,
})

const out = (o) => process.stdout.write(JSON.stringify(o) + '\n')
let pending = null           // {re, timer}

bot.on('message', (jsonMsg) => {
  const text = jsonMsg.toString()
  if (pending && pending.re.test(text)) {
    clearTimeout(pending.timer)
    pending = null
    out({ reply: text })
    return
  }
  out({ chat: text })
})
bot.on('error', (e) => process.stderr.write('bot error: ' + e.message + '\n'))
bot.on('kicked', (r) => { process.stderr.write('bot kicked: ' + r + '\n'); process.exit(1) })
bot.on('end', () => process.exit(0))

bot.once('spawn', () => {
  process.stderr.write(`dcf_bot: spawned as ${bot.username}\n`)
  const rl = readline.createInterface({ input: process.stdin })
  rl.on('line', (line) => {
    let req
    try { req = JSON.parse(line) } catch (e) { return }
    if (!req.cmd) return
    bot.chat('/' + req.cmd)
    if (!req.reply) { out({ reply: '' }); return }
    const re = new RegExp(req.reply)
    const timer = setTimeout(() => { pending = null; out({ reply: '' }) }, (req.timeout || 3) * 1000)
    pending = { re, timer }
  })
  rl.on('close', () => bot.quit())
})
