from __future__ import annotations
import json, re
from google import genai

class Gemini:
 def __init__(self,key,retry,config,voice):
  self.c=genai.Client(api_key=key); self.retry=retry; self.config=config; self.voice=voice
 def text(self,purpose,prompt):
  models=self.config['models'].get(purpose,self.config['models']['script'])
  last=None
  for model in models:
   try:return self.retry(lambda:self.c.models.generate_content(model=model,contents=prompt).text,'gemini',model=model)
   except Exception as e:last=e
  raise last
 def json(self,purpose,prompt):
  txt=self.text(purpose,prompt); m=re.search(r'\{.*\}|\[.*\]',txt,re.S)
  if not m: raise ValueError('Gemini did not return JSON')
  return json.loads(m.group(0))
