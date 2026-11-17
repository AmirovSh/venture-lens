import {cpSync,existsSync,mkdirSync} from 'node:fs';
import {spawn} from 'node:child_process';
if(!existsSync('.next/standalone/server.js'))throw new Error('Сначала выполните npm run build');
mkdirSync('.next/standalone/.next',{recursive:true});
cpSync('.next/static','.next/standalone/.next/static',{recursive:true});
if(existsSync('public'))cpSync('public','.next/standalone/public',{recursive:true});
const server=spawn(process.execPath,['.next/standalone/server.js'],{stdio:'inherit',env:{...process.env,PORT:'3100',HOSTNAME:'127.0.0.1'}});
process.on('SIGINT',()=>server.kill());process.on('SIGTERM',()=>server.kill());
server.on('exit',code=>process.exit(code??1));
