'''class to interpolate position information given a time'''

import sys, os, time, math

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', '..', 'mavlink', 'pymavlink'))
import mavutil, cuav_util

class MavInterpolatorException(Exception):
	'''interpolator error class'''
	def __init__(self, msg):
            Exception.__init__(self, msg)

class MavInterpolatorDeltaTException(MavInterpolatorException):
	'''interpolator error class for over deltat'''
	def __init__(self, msg):
            Exception.__init__(self, msg)

class MavPosition():
	'''represent current position and attitude
	The height is in meters above home ground level
	'''
	def __init__(self, lat, lon, altitude, roll, pitch, yaw, frame_time=None):
		self.lat = lat
		self.lon = lon
		self.altitude = altitude
		self.roll = roll
		self.pitch = pitch
		self.yaw = yaw
		self.time = frame_time

	def __str__(self):
		return 'MavPosition(pos %f %f alt=%.1f roll=%.1f pitch=%.1f yaw=%.1f)' % (
			self.lat, self.lon, self.altitude,
			self.roll, self.pitch, self.yaw)
		
class MavInterpolator():
	'''a class to interpolate position and attitude from a
	series of mavlink messages'''
	def __init__(self, backlog=100):
		self.backlog = backlog
		self.attitude = []
		self.gps_raw = []
		self.vfr_hud = []
		self.scaled_pressure = []
		self.msg_map = {
			'GPS_RAW' : self.gps_raw,
			'ATTITUDE' : self.attitude,
			'VFR_HUD' : self.vfr_hud,
			'SCALED_PRESSURE' : self.scaled_pressure
			}
		self.mlog = None
		self.ground_pressure = None
		self.ground_temperature = None
		self.usec_base = 0


	def _find_msg_idx(self, type, t):
		'''find the msg just before time t'''
		if not type in self.msg_map:
			raise MavInterpolatorException('no msgs of type %s' % type)
		a = self.msg_map[type]
		for i in range(len(a)-1, -1, -1):
			if a[i]._timestamp <= t:
				return i
		raise MavInterpolatorException('no msgs of type %s before %s' % (
			type, time.asctime(time.localtime(t))))

	def _find_msg(self, type, t):
		'''find the msg just before time t'''
		if not type in self.msg_map:
			raise MavInterpolatorException('no msgs of type %s' % type)
		i = self._find_msg_idx(type, t)
		return self.msg_map[type][i]


	def update_usec_base(self, msg):
		'''update the difference between a usec field from
		the APM and message timestamps'''
		pass
				 
	def add_msg(self, msg):
		'''add in a mavlink message'''
		type = msg.get_type()
		if type == 'SCALED_PRESSURE':
			if self.ground_pressure is None:
				self.ground_pressure = msg.press_abs
			if self.ground_temperature is None:
				self.ground_temperature = msg.temperature * 0.01
		if type == 'PARAM_VALUE':
			'''get ground pressure and temperature for altitude'''
			if str(msg.param_id) == 'GND_ABS_PRESS':
				self.ground_pressure = msg.param_value
			if str(msg.param_id) == 'GND_TEMP':
				self.ground_temperature = msg.param_value
		if type in self.msg_map:
			'''add it to the history'''
			self.msg_map[type].append(msg)
			'''keep self.backlog messages around of each type'''
			while len(self.msg_map[type]) > self.backlog:
				self.msg_map[type].pop(0)
		if type == 'ATTITUDE':
			self.update_usec_base(msg)

	def _altitude(self, SCALED_PRESSURE):
		'''calculate barometric altitude relative to the ground'''
		if self.ground_pressure is None:
			self.ground_pressure = SCALED_PRESSURE.press_abs
		if self.ground_temperature is None:
			self.ground_temperature = SCALED_PRESSURE.temperature * 0.01
		scaling = self.ground_pressure / (SCALED_PRESSURE.press_abs*100.0)
		temp = self.ground_temperature + 273.15
		return math.log(scaling) * temp * 29271.267 * 0.001

	def advance_log(self, t):
		'''read from the logfile to advance to time t'''
		if self.mlog is None:
			return
		while True:
			try:
				gps_raw = self._find_msg('GPS_RAW', t)
				attitude = self._find_msg('ATTITUDE', t)
				scaled_pressure = self._find_msg('SCALED_PRESSURE', t)
				if (self.msg_map['GPS_RAW'][-1]._timestamp >= t and
				    self.msg_map['ATTITUDE'][-1]._timestamp >= t and
				    self.msg_map['SCALED_PRESSURE'][-1]._timestamp >= t):
					return
			except MavInterpolatorException:
				pass
			msg = self.mlog.recv_match()
			if msg is None:
				return MavInterpolatorException('end of logfile for timestamp %s' % time.asctime(time.localtime(t)))
			self.add_msg(msg)

	def interpolate(self, type, field, t, max_deltat=0):
		'''find interpolated value for a field'''
		i = self._find_msg_idx(type, t)
		a = self.msg_map[type]
		if i == len(a)-1:
			return getattr(a[i], field)
		v1 = getattr(a[i], field)
		v2 = getattr(a[i+1], field)
		t1 = a[i]._timestamp
		t2 = a[i+1]._timestamp
		if max_deltat != 0 and t2 - t1 > max_deltat:
			raise MavInterpolatorDeltaTException('exceeded max_deltat %.1f' % (t2-t1))
		return v1 + ((t-t1)/(t2-t1))*(v2-v1)
    
	def position(self, t, max_deltat=0):
		'''return a MavPosition estimate given a time'''
		self.advance_log(t)
			
		scaled_pressure = self._find_msg('SCALED_PRESSURE', t)

		# extrapolate our latitude/longitude 
		gps_raw = self._find_msg('GPS_RAW', t)
		(lat, lon) = cuav_util.gps_newpos(gps_raw.lat, gps_raw.lon,
						  gps_raw.hdg,
						  gps_raw.v * (t - gps_raw._timestamp))

		# get altitude
		altitude = self._altitude(scaled_pressure)

		# and attitude
		roll  = math.degrees(self.interpolate('ATTITUDE', 'roll', t, max_deltat))
		pitch = math.degrees(self.interpolate('ATTITUDE', 'pitch', t, max_deltat))
		yaw   = math.degrees(self.interpolate('ATTITUDE', 'yaw', t, max_deltat))

		return MavPosition(lat, lon, altitude, roll, pitch, yaw, t)
	
	def set_logfile(self, filename):
		'''provide a mavlink logfile for data'''
		self.mlog = mavutil.mavlogfile(filename)
		
