# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license. 
# See the NOTICE for more information.
#
import errno
import socket
import traceback
try:
    import ssl
except:
    ssl = None 

from pulsar import system
from pulsar.http.utils import write_nonblock, write_error, close

from .base import Worker
from .task import get_task_loop, start_task_loop


class HttpHandler(object):
    '''Handle HTTP requests and delegate the response to the worker'''
    ALLOWED_ERRORS = (errno.EAGAIN, errno.ECONNABORTED,
                      errno.EWOULDBLOCK, errno.EPIPE)
    ssl_options = None

    def __init__(self, worker):
        self.worker = worker
        
    def __call__(self, fd, events):
        client = None
        try:
            client, addr = self.worker.socket.accept()
            client.setblocking(1)
            req = self.worker.http.Request(client,
                                           addr,
                                           self.worker.address,
                                           self.worker.cfg)
        except socket.error as e:
            close(client)
            if e.errno not in self.ALLOWED_ERRORS:
                raise
            else:
                return
        except StopIteration:
            close(client)
            self.worker.log.debug("Ignored premature client disconnection.")
            return
        
        self.handle(fd, req)

    def handle(self, fd, req):
        self.worker.handle_request(fd, req)
        

class HttpMixin(object):
    '''A Mixin class for handling syncronous connection over HTTP.'''
    ssl_options = None

    def _handle_request(self, req):
        try:
            resp, environ = req.wsgi()
            self.response(resp, environ)
        except StopIteration:
            self.log.debug("Ignored premature client disconnection.")
        except socket.error as e:
            if e[0] != errno.EPIPE:
                self.log.exception("Error processing request.")
            else:
                self.log.debug("Ignoring EPIPE")
        except Exception as e:
            self.log.exception("Error processing request: {0}".format(e))
            try:            
                # Last ditch attempt to notify the client of an error.
                mesg = "HTTP/1.1 500 Internal Server Error\r\n\r\n"
                write_nonblock(req.client_request, mesg)
            except:
                pass
        finally:    
            close(req.client_request)

    def response(self, resp, environ):
        try:
            # Force the connection closed until someone shows
            # a buffering proxy that supports Keep-Alive to
            # the backend.
            resp.force_close()
            respiter = self.handler(environ, resp.start_response)
            for item in respiter:
                resp.write(item)
            resp.close()
            if hasattr(respiter, "close"):
                respiter.close()
        except socket.error:
            raise
        except Exception:
            # Only send back traceback in HTTP in debug mode.
            if not self.debug:
                raise
            write_error(resp.sock, traceback.format_exc())


class Worker(Worker,HttpMixin):
    '''A Http worker on a child process'''
    _class_code = 'HttpProcess'
    
    def _run(self, ioloop = None):
        ioloop = self.ioloop
        handler = HttpHandler(self)
        if ioloop.add_handler(self.socket, handler, ioloop.READ):
            self.socket.setblocking(0)
            ioloop.start()
       
    @classmethod
    def create_socket(cls, address):
        return system.create_socket(address)
    

class HttpPoolHandler(HttpHandler):
    
    def handle(self, fd, req):
        self.worker.putRequest(req)


class _Worker(HttpMixin):
    '''A Http worker on a thread. This worker process http requests from the
pool queue.'''
    _class_code = 'HttpThread'
    
    def get_ioimpl(self):
        return get_task_loop(self)
    
    def _run(self):
        start_task_loop(self)
    
    @classmethod
    def modify_arbiter_loop(cls, wp, ioloop):
        '''The arbiter listen for client connections and delegate the handling
to the Thread Pool. This is different from the Http Worker on Processes'''
        if wp.socket:
            ioloop.add_handler(wp.socket,
                               HttpPoolHandler(wp),
                               ioloop.READ)
            
    @classmethod
    def clean_arbiter_loop(cls, wp, ioloop):
        if wp.socket:
            ioloop.remove_handler(wp.socket)