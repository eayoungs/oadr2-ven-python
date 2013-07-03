# poll.OpenADR2 class
# --------
# Requires the python libXML wrapper "lxml"

import threading, logging
import urllib2
import httplib
import ssl, socket
from lxml import etree
import event, schedule, control

# HTTP parameters:
CONTENT_TYPE = 'application/xml'
DEFAULT_HEADERS = {
    'user-agent': 'EnerNOC VEN',
    'content-type': CONTENT_TYPE
}
REQUEST_TIMEOUT = 5                  # HTTP request timeout
DEFAULT_VTN_POLL_INTERVAL = 300      # poll the VTN every X seconds
OADR2_URI_PATH = 'OpenADR2/Simple/'  # URI of where the VEN needs to request from
CONTROL_LOOP_INTERVAL = 30           # update control state every X second

# A Cipther list.  To configure properly, see: http://www.openssl.org/docs/apps/ciphers.html#CIPHER_LIST_FORMAT
HTTPS_CIPHERS = 'TLS_RSA_WITH_AES_256_CBC_SHA'



class OpenADR2(object):
    '''
    poll.OpenADR2 is the class for sending requests and responses for OpenADR
    2.0 events over HTTP.

    Member Variables:
    --------
    vtn_base_uri
    vtn_poll_interval
    event_handler -- The single instance of the event_handler, gotten via event.get_instace()
    ven_client_cert_key
    ven_client_cert_pem
    vtn_ca_certs 
    test_mode -- Boolean value if we are in test mode or not
    control_thread
    poll_thread
    control_thread -- threading.Thread() object w/ name of 'oadr2.control'
    current_signal_level
    _control -- 
    _control_loop_signal -- threading.Event() object
    _exit -- A threading object via threading.Event()
    '''
   

    def __init__(self, event_config,
                 ven_id, ven_client_cert_key=None, ven_client_cert_pem=None,
                 vtn_base_uri=None, vtn_poll_interval=DEFAULT_VTN_POLL_INTERVAL, vtn_ca_certs=None,
                 test_mode=False, start_thread=True):
        '''
        Sets up the class and intializes the HTTP client.

        event_config -- A dictionary containing key-word arugments for the
                        EventHandller
        ven_id -- ID of the VEN
        ven_client_cert_key -- Certification Key for the HTTP Client
        ven_client_cert_pem -- PEM file/string for the HTTP Client
        vtn_base_uri -- Base URI of the VTN's location
        vtn_poll_interval -- How often we should poll the VTN
        vtn_ca_certs -- CA Certs for the VTN
        test_mode -- If we are in Test Mode or not
        start_thread -- start the thread for the control loop or not?
        '''

        self.vtn_base_uri = vtn_base_uri

        if self.vtn_base_uri: # append path
            join_char = '/' if self.vtn_base_uri[-1] != '/' else ''
            self.vtn_base_uri = join_char.join((self.vtn_base_uri, OADR2_URI_PATH))
        try:
            self.vtn_poll_interval = int(vtn_poll_interval)
        except:
            logging.warn('Invalid poll interval: %s', self.vtn_poll_interval)
            self.vtn_poll_interval = DEFAULT_VTN_POLL_INTERVAL

        self._control_loop_signal = threading.Event()

        self.event_handler = event.get_instance(**event_config)     # Get the instance of the EventHandler

        # Security & Authentication related
        self.ven_client_cert_key = ven_client_cert_key
        self.ven_client_cert_pem = ven_client_cert_pem
        self.vtn_ca_certs = vtn_ca_certs
      
        # Hardware stuff
        self.event_levels = control.event_levels

        self.current_signal_level = 0 
        self.test_mode = bool(test_mode)

        # Set the control interface
        self._control = control.ControlInterface()

        # Add an exit thread for the module
        self._exit = threading.Event()
        self._exit.clear()

        self.control_thread = None
        self.poll_thread = None
        start_thread = bool(start_thread)
        self._init_client(start_thread)

        if start_thread:
            self.control_thread = threading.Thread(
                    name='oadr2.control',
                    target=self.control_event_loop)
            self.control_thread.daemon = True
            self.control_thread.start()

        logging.info( " +++++++++++++++ OADR2 module started ++++++++++++++ " )
        logging.info( ' test mode: %s', self.test_mode )

    
    def _init_client(self, start_thread):
        '''
        Initialize the HTTP client.

        start_thread -- To start the polling thread or not.
        '''
        handlers = []

        if self.ven_client_cert_key:
            logging.debug("Adding HTTPS client cert key: %s, pem: %s", 
                    self.ven_client_cert_key, self.ven_client_cert_pem)
            handlers.append(
                HTTPSClientAuthHandler( 
                    self.ven_client_cert_key,
                    self.ven_client_cert_pem,
                    self.vtn_ca_certs,
                    ssl_version = ssl.PROTOCOL_TLSv1,
                    ciphers = HTTPS_CIPHERS )
            )

        # This is our HTTP client:
        self.http = urllib2.build_opener(*handlers)

        self.poll_thread = threading.Thread(
                name='oadr2.poll',
                target=self.poll_vtn_loop)
        self.poll_thread.daemon = True
        if start_thread:
            self.poll_thread.start()


    def exit(self):
        '''
        Shutdown the HTTP client, join the running threads and exit.
        '''

        self._control_loop_signal.set()     # notify the control loop to exit
        self.control_thread.join(2)
        if self.poll_thread is not None:
            self.poll_thread.join(2)        # they are daemons.
        self._exit.set()
   

    def poll_vtn_loop(self):
        '''
        The threading loop which polls the VTN on an interval 
        '''

        while not self._exit.is_set():
            try:
                self.query_vtn()

            except urllib2.HTTPError as ex: # 4xx or 5xx HTTP response:
                logging.warn("HTTP error: %s\n%s", ex, ex.read())

            except urllib2.URLError, ex: # network error.
                logging.debug("Network error: %s", ex)

            except Exception, ex:
                logging.exception("Error in OADR2 poll thread: %s",ex)

            self._exit.wait(self.vtn_poll_interval)
        logging.info(" +++++++++++++++ OADR2 polling thread has exited." )


    def query_vtn(self):
        '''
        Query the VTN for an event.
        '''

        if not self.vtn_base_uri:
            logging.warn("VTN base URI is invalid: %s", self.vtn_base_uri)
            return

        event_uri = self.vtn_base_uri + 'EiEvent'
        payload = self.event_handler.build_request_payload()

        # Make the request
        req = urllib2.Request(event_uri, etree.tostring(payload), dict(DEFAULT_HEADERS))
        logging.debug('Request:\n%s\n----'%(etree.tostring(payload, pretty_print=True)))
        logging.debug("Request to: %s", req.get_full_url())

        # Get the response
        resp = self.http.open(req, None, REQUEST_TIMEOUT)
        data = resp.read()
        resp.close()
        logging.debug("EiRequestEvent response: %s\n%s", resp.getcode(), data)

        if resp.headers.gettype() != CONTENT_TYPE:
            logging.warn('Unexpected content type')

        reply = None
        try:
            payload = etree.fromstring(data)
            logging.debug('Got Payload:\n%s\n----'%(etree.tostring(payload, pretty_print=True)))
            reply = self.event_handler.handle_payload(payload)
        except:
            logging.warn("error parsing response:\n%s",data)

        # If we have a generated reply:
        if reply is not None:
            logging.debug('Reply:\n%s\n----'%(etree.tostring(reply, pretty_print=True)))
            self._control_loop_signal.set()    # tell the control loop to update control
            self.send_reply(reply, event_uri)  # And send it


    def send_reply(self, payload, uri):
        '''
        Send a reply back to the VTN.

        payload -- An lxml.etree.ElementTree object containing an OpenADR 2.0
                   payload
        uri -- The URI (of the VTN) where the response should be sent
        '''

        request = urllib2.Request(uri, etree.tostring(payload), dict(DEFAULT_HEADERS))
        resp = self.http.open(request,None,REQUEST_TIMEOUT)
        logging.debug("EiEvent response: %s", resp.getcode())


    def control_event_loop(self):
        '''
        This is the threading loop to perform control based on current oadr events
        Note the current implementation simply loops based on CONTROL_LOOP_INTERVAL
        except when an updated event is received by a VTN.
        '''

        self._exit.wait(10)  # give a couple seconds before performing first control

        try:
            self.current_signal_level = self.get_current_relay_level()

        except Exception, ex:
            logging.warn("Error reading initial hardware state! %s",ex)

        while not self._exit.is_set():
            try:
                logging.debug("Updating control states...")
                events = self.event_handler.get_active_events()
                self.do_control(events)

            except Exception as ex:
                logging.exception("Control loop error: %s", ex)

            self._control_loop_signal.wait(CONTROL_LOOP_INTERVAL)
            self._control_loop_signal.clear() # in case it was triggered by a poll update

        logging.info("Control loop exiting.")


    def do_control(self, events):
        '''
        Called by `control_event_loop()` when event states should be updated.
        This parses through the events, and toggles them if they are active. 

        events -- List of lxml.etree.ElementTree objects (with OpenADR 2.0 tags)
        '''
        highest_signal_val = 0
        remove_events = []
        for e in events:
            try:
                e_id = event.get_event_id(e, self.event_handler.ns_map)
                e_mod_num = event.get_mod_number(e, self.event_handler.ns_map)
                e_status = event.get_status(e, self.event_handler.ns_map)

                if not self.event_handler.check_target_info(e):
                    logging.debug("Ignoring event %s - no target match", e_id)
                    continue

                event_start_dttm = event.get_active_period_start(e, self.event_handler.ns_map)
                signals = event.get_signals(e, self.event_handler.ns_map)

                if signals is None:
                    logging.debug("Ignoring event %s - no valid signals", e_id)
                    continue

                logging.debug("All signals: %r", signals)
                intervals = [s[0] for s in signals]
                current_interval = schedule.choose_interval( event_start_dttm, intervals )

                if current_interval is None:
                    logging.debug("Event %s(%d) has ended", e_id, e_mod_num)
                    remove_events.append(e_id)
                    continue

                if current_interval < 0:
                    logging.debug("Event %s(%d) has not started yet.", e_id, e_mod_num)
                    continue

                logging.debug('---------- chose interval %d', current_interval)
                _, interval_uid, signal_level = signals[current_interval]
#                signal_level = event.get_current_signal_value(e, self.event_handler.ns_map)

                logging.debug('Control loop: Evt ID: %s(%s); Interval: %s; Current Signal: %s',
                        e_id, e_mod_num, interval_uid, signal_level )
                
                signal_level = float(signal_level) if signal_level is not None else 0
                highest_signal_val = max(signal_level, highest_signal_val)

            except Exception as e:
                logging.exception("Error parsing event: %s", e)

        self.event_handler.remove_events(remove_events)

        logging.debug("Highest signal level is: %f", highest_signal_val)
        
        self.toggle_relays(highest_signal_val)

    
    def toggle_relays(self, signal_level):
        '''
        Toggles the relays on the hardware (via the control interface).

        signal_level -- If it is the same as the current signal level, the
                        function will exit.  Else, it will change the
                        signal relay
        '''

        # Run a check
        signal_level = float(signal_level)
        if signal_level == self.current_signal_level:
            return

        for i in range(len(self.event_levels)):
            control_val = 1 if i < signal_level else 0
            self._control.do_control_point('control_set', self.event_levels[i], control_val)

        self.current_signal_level = signal_level


    def get_current_relay_level(self):
        '''
        Gets the current relay levels for us (from the control interface).

        Returns: '0' if all control points are at '0'.  Any non-Zero number
                  otherwise.
        '''

        for i in xrange(len(self.event_levels),0,-1):
             val = self._control.do_control_point('control_get', self.event_levels[i-1])
             if val:
                return i

        return 0


# http://stackoverflow.com/questions/1875052/using-paired-certificates-with-urllib2
class HTTPSClientAuthHandler(urllib2.HTTPSHandler):
    '''
    Allows sending a client certificate with the HTTPS connection.
    This version also validates the peer (server) certificate.
    '''

    def __init__(self, key, cert, ca_certs, ssl_version=None, ciphers=None):
        '''
        Intiailize the Client Authentication Handler
        
        key -- An encryption key
        cert -- A Certificate
        ca_certs -- CA Certificates
        ssl_version -- What version of SSL are we using
        ciphers -- What encryption method
        '''

        urllib2.HTTPSHandler.__init__(self)
        self.key = key
        self.cert = cert
        self.ca_certs = ca_certs
        self.ssl_version = ssl_version
        self.ciphers = ciphers


    def https_open(self, req):
        '''
        Open a connection.
        Rather than pass in a reference to a connection class, we pass in
        a reference to a function which, for all intents and purposes,
        will behave as a constructor

        req -- A request
        '''

        return self.do_open(self.get_connection, req)


    def get_connection(self, host, timeout=REQUEST_TIMEOUT):
        '''
        Gets a HTTPS connection.

        host -- What host
        timeout -- How long to wait.

        Return HTTPSConnection object
        '''

        return HTTPSConnection( host, 
                key_file = self.key, 
                cert_file = self.cert,
                timeout = timeout,
                ciphers = self.ciphers,
                ca_certs = self.ca_certs, 
                ssl_version = self.ssl_version )



class HTTPSConnection(httplib.HTTPSConnection):
    '''
    Overridden to allow peer certificate validation, configuration
    of SSL/ TLS version and cipher selection.  See:
    http://hg.python.org/cpython/file/c1c45755397b/Lib/httplib.py#l1144
    and `ssl.wrap_socket()`
    '''

    def __init__(self, host, **kwargs):
        '''
        Initilize the HTTPS conneciton

        host -- See httplib.HTTPSConnection's documentation
        kwargs -- See httplib.HTTPSConnection's documentation
        '''

        self.ciphers = kwargs.pop('ciphers',None)
        self.ca_certs = kwargs.pop('ca_certs',None)
        self.ssl_version = kwargs.pop('ssl_version',ssl.PROTOCOL_SSLv23)

        httplib.HTTPSConnection.__init__(self,host,**kwargs)

    def connect(self):
        '''
        Connect to the server.
        '''

        sock = socket.create_connection( (self.host, self.port), self.timeout )

        if self._tunnel_host:
            self.sock = sock
            self._tunnel()

        if self.ca_certs:
            with open(self.ca_certs,'r') as test:
                logging.info('+++++++++++++++ CA CERTS: %s ++++++++++++++', self.ca_certs)
            
        self.sock = ssl.wrap_socket( sock, 
                self.key_file, self.cert_file,
                ca_certs = self.ca_certs,
                ciphers = self.ciphers,  # NOTE: This is Python 2.7-only!
                cert_reqs = ssl.CERT_REQUIRED if self.ca_certs else ssl.CERT_NONE,
                ssl_version = self.ssl_version )



