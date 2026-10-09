"""Own normalized public event values; executable cases derive them from native reads."""
import copy

def scan(root,turn=None,direction='asc'):
    return {'thread_id':root,'turn_id':turn,'sort_direction':direction,'from_cursor':None,'final_cursor':None,'complete':True}

def history(root,client,turn,item,content):
    return {'type':'input/history','client_message_id':client,'thread_id':root,'turn_id':turn,'item_id':item,'input':copy.deepcopy(content),'scan':scan(root,turn)}

def normal_end(root,client,turn,item,content,source='thread/read'):
    event={'type':'input/history-settled','client_message_id':client,'thread_id':root,'turn_id':turn,'item_id':item,'normal_end':{'status':'completed','error':None,'items_view':'full','source':source,'matching_item':{'type':'userMessage','id':item,'client_id':client,'input':copy.deepcopy(content)}}}
    if source=='thread/turns/list':event['normal_end']['turn_scan']=scan(root)
    return event
