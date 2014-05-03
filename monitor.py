from functions import *

init_auth_browser()

last_task = None
task = None
mailed = False

def do_get_class(class_id):
	i = 1
	while i <= 5:
		try:
			return get_class(class_id)
		except AttributeError:
			init_auth_browser()
			time.sleep(.5*i)
			i += 1
		except Exception:
			time.sleep(1*i)
			i += 1
	queue.insert(task)

def error_mail():
	message = sendgrid.Mail()
	message.add_to(os.getenv('admin_email'))
	message.set_subject('Stuck Worker @ MIT Textbooks')
	message.set_html('<pre>' + repr(task) + '</pre>')
	message.set_text(repr(task))
	message.set_from('MIT Textbooks <tb_support@mit.edu>')
	try:
		print sg.send(message)
	except Exception, e:
		print str(e)
		print 'Stuck Worker'
		print repr(task)

while True:
	last_task = task
	task = queue.find_one(sort=[("time", 1)])
	if task == last_task and not mailed:
		mailed = True
		error_mail()
	if task:
		queue.remove(task)
		_id = task['class_id']
		print 'Processing {_id}'.format(_id=_id)
		if task['group']:
			group_obj = get_group(_id)
			for c in group_obj.class_ids:
				do_get_class(c)
		elif task['update']:
			update_textbooks(_id)
		else:
			do_get_class(_id)
	else:
		time.sleep(5)