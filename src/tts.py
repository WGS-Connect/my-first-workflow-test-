from __future__ import annotations
import asyncio, subprocess, tempfile
from pathlib import Path
import edge_tts

def _edge_rate(speed):
 # Edge rate is expressed as a percentage relative to normal speech.
 pct=round((float(speed)-1.0)*100)
 return f"{pct:+d}%"

def _edge_pitch(pitch):
 return f"{float(pitch):+g}Hz"

class TTS:
 def __init__(self,retry,voice): self.retry=retry; self.v=voice
 def edge(self,text,out):
  async def run():
   with tempfile.NamedTemporaryFile(suffix='.mp3',delete=False) as f: tmp=f.name
   try:
    speed=self.v.get('speed',1.0); pitch=self.v.get('pitch',0)
    rate=self.v.get('edge_tts',{}).get('rate') or _edge_rate(speed)
    if 'speed' in self.v: rate=_edge_rate(speed)
    epitch=self.v.get('edge_tts',{}).get('pitch') or _edge_pitch(pitch)
    if 'pitch' in self.v: epitch=_edge_pitch(pitch)
    voice=self.v['edge_tts']['voice']
    c=edge_tts.Communicate(text,voice,rate=rate,pitch=epitch); await c.save(tmp)
    subprocess.run(['ffmpeg','-y','-i',tmp,'-c:a','pcm_s16le',str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   finally: Path(tmp).unlink(missing_ok=True)
  self.retry(lambda:asyncio.run(run()),'edge_tts',model=self.v['edge_tts']['voice']); return out
 def kokoro(self,text,out):
  def run():
   from kokoro import KPipeline
   import soundfile as sf, numpy as np
   lang=self.v.get('language','en-US').split('-')[0]; p=KPipeline(lang_code=lang); chunks=[]
   speed=float(self.v.get('speed',self.v.get('kokoro',{}).get('speed',1.0)))
   voice=self.v['kokoro']['voice']
   for _,_,audio in p(text,voice=voice,speed=speed): chunks.append(audio)
   if not chunks: raise RuntimeError('Kokoro returned no audio')
   sf.write(out,np.concatenate(chunks),24000)
  self.retry(run,'kokoro',model=self.v['kokoro']['voice']); return out
 def make(self,text,out,provider=None):
  order=['edge_tts','kokoro']
  if provider and provider in order: order=order[order.index(provider):]
  last=None
  for p in order:
   try:
    if p=='edge_tts': self.edge(text,out)
    else:self.kokoro(text,out)
    return p
   except Exception as e:last=e
  raise last
