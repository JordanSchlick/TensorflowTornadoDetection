import glob
import os
import sys
import random
import threading
import time
import math
import multiprocessing
import hashlib

import numpy as np
import tensorflow as tf
import utils
import tornado_data

sys.path.insert(0, '../')
import openstorm_radar_py

class ThreadedDataset:
	def __init__(self,thread_count=1,buffer_size=5,unpack_lists=True,log_queue_empty=False,debug_name="ThreadedDataset") -> None:
		"""A multithreaded base for piplined datasets
		Needs to be subclassesd and implement the _generator function to be used
		Args:
			thread_count (int, optional): Number of threads to use. Defaults to 1.
			buffer_size (int, optional): Number of items to be queued up. Defaults to 5.
			unpack_lists (bool, optional): If elements of lists returned by _generator should be returned individually from next
		"""
		self.thread_count = thread_count
		self.buffer_size = buffer_size
		self.unpack_lists = unpack_lists
		self.log_queue_empty = log_queue_empty
		self.debug_name = debug_name
		self.buffer_event = threading.Condition()
		self.buffer_lock = threading.Lock()
		self.buffer = []
		self.alive = True
		if thread_count > 0:
			for i in range(thread_count):
				t = threading.Thread(target=self._read_thread)
				t.daemon = True
				t.start()
	
	def destroy(self):
		self.alive = False
		self.buffer.clear()
	
	def _read_thread(self):
		"""Runs the generator function if data is needed
		"""
		while threading.main_thread().is_alive() and self.alive:
			self.buffer_lock.acquire()
			current_buffer_size = len(self.buffer)
			self.buffer_lock.release()
			if current_buffer_size >= self.buffer_size:
				if not self.alive:
					return
				time.sleep(0.05)
				continue
			buffer = self._generator()
			self.buffer_lock.acquire()
			if not self.alive:
				self.buffer_lock.release()
				return
			if isinstance(buffer, list) and self.unpack_lists:
				self.buffer.extend(buffer)
			else:
				self.buffer.append(buffer)
			self.buffer_lock.release()
			
			self.buffer_event.acquire()
			self.buffer_event.notify(1)
			self.buffer_event.release()
	
	
	def size(self):
		self.buffer_lock.acquire()
		current_buffer_size = len(self.buffer)
		self.buffer_lock.release()
		return current_buffer_size
	
	
	def next(self,blocking=True):
		"""Gets the next item

		Returns:
			any: An item generated by _generator
		"""
		
		#print("=="+self.debug_name)
		if self.thread_count > 0:
			while True:
				self.buffer_lock.acquire()
				current_buffer_size = len(self.buffer)
				self.buffer_lock.release()
				if current_buffer_size > 0:
					self.buffer_lock.acquire()
					buffer = self.buffer.pop(0)
					self.buffer_lock.release()
					return buffer
				elif blocking:
					if self.log_queue_empty:
						print("Empty queue "+self.debug_name)
					self.buffer_event.acquire()
					self.buffer_event.wait()
					self.buffer_event.release()
				else:
					return None
		else:
			while len(self.buffer) == 0:
				buffer = self._generator()
				if isinstance(buffer, list) and self.unpack_lists:
					self.buffer.extend(buffer)
				else:
					self.buffer.append(buffer)
			
			buffer = self.buffer.pop(0)
			return buffer






class DirectoryTrainTest:
	def __init__(self, folder, train_percentage=80):
		"""Deterministically splits files in a directory into train test lists 

		Args:
			folder (string): Path to folder to list files from
			train_percentage (int, optional): Percentage of the files that will be part of the training set. Defaults to 80.
		"""
		self.folder = folder
		files = glob.glob(folder + '/*', recursive = True)
		try:
			files.remove(".gitkeep")
		except ValueError:
			pass
		print("found "+str(len(files))+" files")
		files = map(self._process_file, files)
		files = sorted(files, key=lambda d: d["hash"])
		split_location = round(len(files) * train_percentage / 100)
		
		self.train_info = files[0:split_location]
		self.test_info = files[split_location:]
		self.train_list = list(map(lambda x: x["name"], self.train_info))
		self.test_list = list(map(lambda x: x["name"], self.test_info))
	
	def _process_file(self, filename):
		m = hashlib.sha256()
		m.update(filename.encode())
		return {
			"name": filename, 
			"path": self.folder + "/" + filename,
			"hash": m.hexdigest()
		}










class TornadoDataset(ThreadedDataset):
	def __init__(self,files,auto_shuffle=False,thread_count=4,buffer_size=5,section_size=512,log_queue_empty=False) -> None:
		"""A dataset for finding tornados in radar data

		Args:
			files (list[str]): List of radar files.
			auto_shuffle (bool, optional): Automatically shuffle the data. Defaults to False.
			thread_count (int, optional): Number of threads to use. Defaults to 0.
			buffer_size (int, optional): Number of items to be queued up. Defaults to 5.
			section_size (int, optional): Size of outputs along theta and sweep
		"""
		self.files = files
		self.auto_shuffle = auto_shuffle
		self.section_size = section_size
		self.location = 0
		
		# ensure the data is loaded
		tornado_data.get_all_tornados()
		
		super().__init__(thread_count=thread_count,buffer_size=buffer_size,log_queue_empty=log_queue_empty,debug_name="TornadoDataset")
		if self.auto_shuffle:
			self.shuffle()
	
	def _slice_buffer(self, buffer, theta_start, theta_end, radius_start, radius_end, padded=False):
		"""Slice a section out of a buffer"""
		theta_length = buffer.shape[1 if len(buffer.shape) >= 3 else 0]
		if theta_start < 0:
			theta_start += theta_length
			theta_end += theta_length
		if theta_start >= theta_length:
			theta_start -= theta_length
			theta_end -= theta_length
		theta_slice = np.arange(theta_start, theta_end)
		if padded:
			theta_slice = theta_slice + 1
			theta_slice = np.where(theta_slice < (theta_length - 1), theta_slice, theta_slice - (theta_length - 2))
		theta_slice = theta_slice % theta_length
		if len(buffer.shape) >= 3:
			# discard higher sweeps because they are unlikely to contain useful information about the tornado
			return buffer[0:8,theta_slice,radius_start:radius_end]
		else:
			return buffer[theta_slice,radius_start:radius_end]
	
	def _validate_radar_data(self, radar_data):
		"""A quick test to see if radar data is missing significant amounts of data"""
		sweep_info = radar_data.get_sweep_info()
		sweep_count = 0
		for sweep in sweep_info:
			if sweep["id"] != -1:
				sweep_count += 1
		if sweep_count < 5:
			return False
		if sweep_info[0]["actual_ray_count"] < 700:
			return False
		return True
		
	
	def _generator(self):
		"""Loads a file

		Returns:
			list[dict]: radar data and masks
		"""
		data = None
		while data is None:
			self.buffer_lock.acquire()
			file = self.files[self.location]
			self.location += 1
			if self.location >= len(self.files):
				self.location = 0
				self.buffer_lock.release()
				if self.auto_shuffle:
					self.shuffle()
			else:
				self.buffer_lock.release()
			
			radar_data_holder = openstorm_radar_py.RadarDataHolder()
			
			# select products to load
			reflectivity_product = radar_data_holder.get_product(openstorm_radar_py.VolumeTypes.VOLUME_REFLECTIVITY)
			velocity_product = radar_data_holder.get_product(openstorm_radar_py.VolumeTypes.VOLUME_STORM_RELATIVE_VELOCITY)
			spectrum_width_product = radar_data_holder.get_product(openstorm_radar_py.VolumeTypes.VOLUME_SPECTRUM_WIDTH)
			corelation_coefficient_product = radar_data_holder.get_product(openstorm_radar_py.VolumeTypes.VOLUME_CORELATION_COEFFICIENT)
			differential_reflectivity_product = radar_data_holder.get_product(openstorm_radar_py.VolumeTypes.VOLUME_DIFFERENTIAL_REFLECTIVITY)
			
			# load radar data
			radar_data_holder.load(file)
			while(radar_data_holder.get_state() == openstorm_radar_py.RadarDataHolder.DataStateLoading):
				#print("loading...", end='\r')
				time.sleep(0.1)
			#print("loaded", "      ", end='\n')
			
			# check that all products are loaded
			is_fully_loaded = reflectivity_product.is_loaded() and velocity_product.is_loaded() and spectrum_width_product.is_loaded() and corelation_coefficient_product.is_loaded() and differential_reflectivity_product.is_loaded()
			if not is_fully_loaded:
				print("could not load all products from", file)
				continue
			
			# get data for each product
			reflectivity_data = reflectivity_product.get_radar_data()
			velocity_data = velocity_product.get_radar_data()
			spectrum_width_data = spectrum_width_product.get_radar_data()
			corelation_coefficient_data = corelation_coefficient_product.get_radar_data()
			differential_reflectivity_data = differential_reflectivity_product.get_radar_data()
			
			if not self._validate_radar_data(reflectivity_data):
				print("missing data in reflectivity volume", file)
				continue
			if not self._validate_radar_data(velocity_data):
				print("missing data in velocity volume", file)
				continue
			
			theta_start = 0
			theta_end = 512
			radius_start = 0 + 5
			radius_end = 512 + 5
			
			# buffers = [
			# 	self._slice_buffer(np.array(reflectivity_data.buffer), theta_start, theta_end, radius_start, radius_end, padded=True),
			# 	self._slice_buffer(np.array(velocity_data.buffer), theta_start, theta_end, radius_start, radius_end, padded=True),
			# 	self._slice_buffer(np.array(spectrum_width_data.buffer), theta_start, theta_end, radius_start, radius_end, padded=True),
			# 	self._slice_buffer(np.array(corelation_coefficient_data.buffer), theta_start, theta_end, radius_start, radius_end, padded=True),
			# 	self._slice_buffer(np.array(differential_reflectivity_data.buffer), theta_start, theta_end, radius_start, radius_end, padded=True),
			# ]
			
			# convert data to numpy arrays
			buffers = [
				np.array(reflectivity_data.buffer),
				np.array(velocity_data.buffer),
				np.array(spectrum_width_data.buffer),
				np.array(corelation_coefficient_data.buffer),
				np.array(differential_reflectivity_data.buffer)
			]
			
			# get tornado mask
			mask, tornados = tornado_data.generate_mask(reflectivity_data)
			
			# allow data to be freed quicker
			reflectivity_data = None
			velocity_data = None
			spectrum_width_data = None
			corelation_coefficient_data = None
			differential_reflectivity_data = None
			reflectivity_product = None
			velocity_product = None
			spectrum_width_product = None
			corelation_coefficient_product = None
			differential_reflectivity_product = None
			radar_data_holder = None
			
			outputs = []
			
			# generate output for each tornado in mask
			for tornado in tornados:
				sliced_buffers = []
				theta_start = round(tornado["location_theta"])
				radius_start = round(tornado["location_radius"])
				
				# random offset for section
				theta_start += random.randint(-self.section_size / 4, self.section_size / 4) - round(self.section_size / 2)
				radius_start += random.randint(-self.section_size / 4, self.section_size / 4) - round(self.section_size / 2)
				theta_end = theta_start + self.section_size
				if radius_start < 5:
					radius_start = 5
				if radius_start + self.section_size > buffers[0].shape[2]:
					radius_start = buffers[0].shape[2] - self.section_size
				radius_end = radius_start + self.section_size
				
				#print(theta_start, theta_end, radius_start, radius_end)
				
				# slice buffers
				for buffer in buffers:
					sliced_buffers.append(self._slice_buffer(buffer, theta_start, theta_end, radius_start, radius_end, padded=True))
				sliced_mask = self._slice_buffer(mask, theta_start, theta_end, radius_start, radius_end, padded=False)
				
				outputs.append({
					"data": np.stack(sliced_buffers, axis=-1),
					"mask": sliced_mask,
					"file": file,
					"bounds": (theta_start, theta_end, radius_start, radius_end)
				})
			
			#buffer = np.stack(buffers, axis=-1)
			data = outputs
				
		return data
	
	
	
	def shuffle(self):
		"""Shuffles the data set
		"""
		self.buffer_lock.acquire()
		random.shuffle(self.files)
		self.buffer = []
		self.location = 0
		self.buffer_lock.release()













class TensorFlowDataset():
	def __init__(self,from_dataset,thread_count=2,buffer_size=5,batch_size=1,log_queue_empty=False) -> None:
		"""Gets the data ready to be consumed by a tensorflow training loop

		Args:
			training_dataset (TrainingDataset): Training dataset to use
			thread_count (int, optional): Number of threads to use. Defaults to 2.
			buffer_size (int, optional):  Number of items to be queued up. Defaults to 5.
			batch_size (int, optional): Output batch size. Defaults to 1.
		"""
		self.from_dataset = from_dataset
		self.batch_size = batch_size
		self.log_queue_empty = log_queue_empty
		example = self._read()
		defined_names_list = []
		defined_type_list = []
		for key in example:
			item = example[key]
			defined_names_list.append(key)
			shape = item.shape
			shape = tf.TensorSpec(shape,item.dtype)
			defined_type_list.append(item.dtype)
			
		self.queue = tf.queue.FIFOQueue(buffer_size,defined_type_list,names=defined_names_list)
		if thread_count > 0:
			for i in range(thread_count):
				t = threading.Thread(target=self._read_thread)
				t.daemon = True
				t.start()
		
	def _read(self):
		"""reads one item

		Returns:
			dict: Dictionary containing data
		"""
		out_dict = {}
		in_dict = self.from_dataset.next()
		for key in in_dict:
			# filter out bad types
			if not isinstance(in_dict[key], str):
				out_dict[key] = in_dict[key]
		return 
		
		
	def _read_thread(self):
		"""reads items into queue
		"""
		while threading.main_thread().is_alive():
			out_items = []
			for i in range(self.batch_size):
				out_items.append(self._read())
			if self.batch_size == 1:
				self.queue.enqueue(out_items[0])
			else:
				self.queue.enqueue(self._concat_dict(out_items))
	
	def _concat_dict(self,items,axis=0):
		"""Runs tf.concat on a dictionary

		Args:
			items (list<dict>): List of dictionaries with same shape
			axis (int, optional): Axis to concat on. Defaults to 0.

		Returns:
			dict: Concatinated dictionary
		"""
		out_dictionary = {}
		for dictionary in items:
			for key in dictionary:
				if key not in out_dictionary:
					out_dictionary[key] = []
				out_dictionary[key].append(dictionary[key])
		for key in out_dictionary:
			out_dictionary[key] = tf.concat(out_dictionary[key],axis)
		return out_dictionary
	
	def next(self):
		"""Gets one item

		Returns:
			dict: Dictionary containing data
		"""
		# data = self.queue.dequeue_many(self.batch_size)
		# print("get next")
		
		# if self.log_queue_empty and self.queue.size() < self.batch_size:
		# 	print("Empty queue TrainingDatasetTensorFlow")
		# if self.batch_size > 1:
		# 	data = self._concat_dict([self.queue.dequeue() for i in range(self.batch_size)])
		# else:
		# 	data = self.queue.dequeue()
		
		if self.log_queue_empty and self.queue.size() < 1:
			print("Empty queue TrainingDatasetTensorFlow")
		data = self.queue.dequeue()
		
		#data = tf.concat([self.queue.dequeue(), self.queue.dequeue()],axis=0)
		# print("got next")
		# print(data)
		return data
		# return self.queue.dequeue()
	
	def _generator(self):
		"""Generator for tensorflow dataset

		Yields:
			dict: Dictionary containing data
		"""
		while True:
			yield self.next()
	
	def tf_dataset(self):
		"""Creates a tf.data.Dataset backed by this dataset to be used with tensorflow apis

		Returns:
			tf.data.Dataset: Tensorflow dataset
		"""
		example = self.next()
		defined_shape_dict = {}
		for key in example:
			item = example[key]
			shape = tf.TensorSpec(item.shape,item.dtype)
			defined_shape_dict[key] = shape
		print(defined_shape_dict)
		return tf.data.Dataset.from_generator(
            self._generator,
            output_signature = defined_shape_dict
        )