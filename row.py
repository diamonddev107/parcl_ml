#!/usr/bin/env python
# * coding: utf8 *
"""
UDOT Right of Way (ROW) Parcel Number Extraction
Right of way module containing methods
"""
import logging
import math
from io import BytesIO
from itertools import islice
from os import environ
from pathlib import Path
from time import perf_counter

import cv2
import google.cloud.documentai
import google.cloud.logging
import google.cloud.storage
import numpy as np
import pandas as pd
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import InternalServerError, InvalidArgument, RetryError
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
from PIL.Image import DecompressionBombError

if "PY_ENV" in environ and environ["PY_ENV"] == "production":
    LOGGING_CLIENT = google.cloud.logging.Client()
    STORAGE_CLIENT = google.cloud.storage.Client()

    LOGGING_CLIENT.setup_logging()

TASK_RESULTS = []


def mosaic_all_circles(job_name, input_bucket, output_location, file_index, task_index, task_count, total_size):
    """the code to run in the cloud run job

    Args:
        job_name (str): the name of the run job. typically named after an animal in alphabetical order
        input_bucket (str): the bucket to get files from using the format `gs://bucket-name`
        output_location (str): the location to save the results to. omit the `gs://` prefix
        file_index (str): the path to the folder containing an `index.txt` file listing all the images in a bucket.
                          `gs://bucket-name`
        task_index (int): the index of the task running
        task_count (int): the number of containers running the job
        total_size (int): the total number of files to process

    Returns:
        None
    """
    #: Get files to process for this job
    files = get_files_from_index(file_index, task_index, task_count, total_size)
    logging.info("job name: %s task: %i processing %s files", job_name, task_index, files)

    #: Initialize GCP storage client and bucket
    bucket = STORAGE_CLIENT.bucket(input_bucket[5:])

    #: Iterate over objects to detect circles and perform OCR
    for object_name in files:
        object_start = perf_counter()
        object_name = object_name.rstrip()
        extension = Path(object_name).suffix.casefold()

        if extension == ".pdf":
            conversion_start = perf_counter()
            images, count, messages = convert_pdf_to_jpg_bytes(
                bucket.blob(object_name).download_as_bytes(), object_name
            )
            logging.info(
                "job name: %s task: %i conversion time %s: %s",
                job_name,
                task_index,
                format_time(perf_counter() - conversion_start),
                {"file": object_name, "pages": count, "message": messages},
            )

        elif extension in [".jpg", ".jpeg", ".tif", ".tiff", ".png"]:
            images = list([bucket.blob(object_name).download_as_bytes()])
        else:
            logging.info('job name: %s task: %i not a valid document or image: "%s"', job_name, task_index, object_name)

            continue

        #: Process images to get detected circles
        logging.info("job name: %s task: %i detecting circles in %s", job_name, task_index, object_name)
        all_detected_circles = []
        circle_start = perf_counter()

        for image in images:
            circle_images = get_circles_from_image_bytes(image, None, object_name)
            all_detected_circles.extend(circle_images)  #: extend because circle_images will be a list

        logging.info(
            "job name: %s task: %i circle detection time taken %s: %s",
            job_name,
            task_index,
            object_name,
            format_time(perf_counter() - circle_start),
        )

        circle_count = len(all_detected_circles)
        if circle_count == 0:
            logging.warning("job name: %s task: %i 0 circles detected in %s", job_name, task_index, object_name)

        #: Process detected circle images into a mosaic
        logging.info("job name: %s task: %i mosaicking images in %s", job_name, task_index, object_name)
        mosaic_start = perf_counter()

        mosaic = build_mosaic_image(all_detected_circles, object_name, None)

        logging.info(
            "job name: %s task: %i image mosaic time taken %s: %s",
            job_name,
            task_index,
            object_name,
            format_time(perf_counter() - mosaic_start),
        )

        logging.info(
            "job name: %s task: %i total time taken for entire task %s",
            job_name,
            task_index,
            format_time(perf_counter() - object_start),
        )

        upload_mosaic(mosaic, output_location, object_name, job_name)


def ocr_all_mosaics(inputs):
    """the code to run in the cloud run job

    Args:
        inputs (class): the inputs to the function
            job_name (str): the name of the run job. typically named after an animal in alphabetical order
            input_bucket (str): the bucket to get files from using the format `gs://bucket-name`
            output_location (str): the location to save the results to. omit the `gs://` prefix
            file_index (str): the path to the folder containing an `index.txt` file listing all the images in a bucket.
                            `gs://bucket-name`
            task_index (int): the index of the task running
            task_count (int): the number of containers running the job
            total_size (int): the total number of files to process
            project_number (int): the number of the gcp project
            processor_id (str): the id of the documentai processor

    Returns:
        A list of lists with the results of the OCR
    """
    #: Get files to process for this job
    files = get_files_from_index(inputs.file_index, inputs.task_index, inputs.task_count, inputs.total_size)
    logging.info("job name: %s task: %i processing %s files", inputs.job_name, inputs.task_index, files)

    #: Initialize GCP storage client and bucket
    bucket = STORAGE_CLIENT.bucket(inputs.input_bucket[5:])

    options = ClientOptions(api_endpoint="us-documentai.googleapis.com")
    ai_client = google.cloud.documentai.DocumentProcessorServiceClient(client_options=options)

    processor_name = ai_client.processor_path(inputs.project_number, "us", inputs.processor_id)

    #: Iterate over objects to detect circles and perform OCR
    for object_name in files:
        object_start = perf_counter()
        object_name = object_name.rstrip()

        image_content = bucket.blob(object_name).download_as_bytes()

        logging.info(
            "job name: %s task: %i download finished %s: %s",
            inputs.job_name,
            inputs.task_index,
            format_time(perf_counter() - object_start),
            {"file": object_name},
        )

        raw_document = google.cloud.documentai.RawDocument(content=image_content, mime_type="image/jpeg")
        request = google.cloud.documentai.ProcessRequest(name=processor_name, raw_document=raw_document)

        result = None
        try:
            result = ai_client.process_document(request=request)
            logging.info(
                "job name: %s task: %i ocr finished %s: %s",
                inputs.job_name,
                inputs.task_index,
                format_time(perf_counter() - object_start),
                {"file": object_name},
            )
        except (RetryError, InternalServerError) as error:
            logging.warning(
                "job name: %s task %i: ocr failed on %s. %s",
                inputs.job_name,
                inputs.task_index,
                object_name,
                error.message,
            )

            continue
        except (InvalidArgument) as error:
            logging.warning(
                "job name: %s task %i: ocr failed on %s. %s\n%s",
                inputs.job_name,
                inputs.task_index,
                object_name,
                error.message,
                error.details,
            )

            continue

        TASK_RESULTS.append([object_name, result.document.text])

    upload_results(TASK_RESULTS, inputs.output_location, f"task-{inputs.task_index}", inputs.job_name)

    return TASK_RESULTS


def generate_index(from_location, prefix, save_location):
    """reads file names from the `from_location` and optionally saves the list to the `save_location` as an index.txt
    file. Prefix can optionally be included to narrow down index location. Cloud storage buckets must start with `gs://`
    Args:
        from_location (str): the directory to read the files from. Prefix GSC buckets with gs://.
        prefix (str): subdirectory or GCS prefix. This prefix will also be stripped from the beginning of GCS paths.
        save_location (str): the directory to save the list of files to. An index.txt file will be created within this
                             directory
    Returns:
        list(str): a list of file names
    """

    files = list([])

    logging.info('reading files from "%s"', from_location)
    if from_location.startswith("gs://"):
        iterator = STORAGE_CLIENT.list_blobs(from_location[5:], max_results=None, versions=False, prefix=prefix)

        files = [blob.name.removeprefix(prefix).strip() for blob in iterator]
    else:
        from_location = Path(from_location)

        if prefix:
            from_location = from_location / prefix

        if not from_location.exists():
            logging.warning("from location %s does not exists", from_location)

            return files

        iterator = from_location.glob("**/*")

        files = [str(item).strip() for item in iterator if item.is_file()]

    if save_location is None:
        return files

    if save_location.startswith("gs://"):
        bucket = STORAGE_CLIENT.bucket(save_location[5:])
        blob = bucket.blob("index.txt")

        with BytesIO() as data:
            for item in files:
                data.write(str(item).encode("utf-8") + b"\n")

            blob.upload_from_string(data.getvalue())
    else:
        save_location = Path(save_location)

        if not save_location.exists():
            save_location.mkdir(parents=True, exist_ok=True)

        with save_location.joinpath("index.txt").open("w", encoding="utf-8", newline="") as output:
            for item in files:
                output.write(str(item) + "\n")

    return files


def download_file_from(bucket_name, file_name):
    """downloads `file_name` from `bucket_name`. Index path object is returned.
    Cloud storage buckets must start with `gs://`
    Args:
        bucket_name (str): the bucket where the file resides. Prefix GSC buckets with gs://.
        file_name (number): the file name (with extension, i.e. index.txt) to download
    Returns:
        index (Path): path object for the downloaded file
    """
    index = Path(__file__).parent / ".ephemeral" / file_name

    if not bucket_name.startswith("gs://"):
        logging.warning("bucket name %s does not start with gs://", bucket_name)

        return None

    bucket = STORAGE_CLIENT.bucket(bucket_name[5:])

    blob = bucket.blob(file_name)

    if not index.parent.exists():
        index.parent.mkdir(parents=True)

    try:
        blob.download_to_filename(str(index))
    except Exception as ex:
        logging.error("error downloading file index %s. %s", index, ex, exc_info=True)

        raise ex

    return index


def get_index(from_location):
    """generic function to get index from cloud storage or local directory. Index path object is returned.
    Cloud storage buckets must start with `gs://`
    Args:
        from_location (str): the bucket or local directory where the index.txt file resides.
                             Prefix GSC buckets with gs://.
    Returns:
        index (Path): path object for the index.txt file
    """

    if from_location.startswith("gs://"):
        index = download_file_from(from_location, "index.txt")
    else:
        folder = Path(from_location)

        if not folder.exists():
            raise FileNotFoundError("folder does not exist")

        index = folder.joinpath("index.txt")

        if not index.exists():
            raise FileNotFoundError("index.txt file does not exist")

    return index


def get_first_and_last_index(task_index, task_count, total_size):
    """calculates a range of indexes based on the task index and total number of tasks. This is used to split up the
    index file
    Args:
        task_index (number): the index of the current cloud run task
        task_count (number): the total number of cloud run tasks
        total_size (number): the total number of files to process

    Returns:
        tuple(number, number): the first index and last index
    """
    job_size = math.ceil(total_size / task_count)
    first_index = task_index * job_size
    last_index = task_index * job_size + job_size

    return first_index, last_index


def get_files_from_index(from_location, task_index, task_count, total_size):
    """reads the index.txt file from the `from_location`. Based on the task index and total task count a list of files
    is returned. Cloud storage buckets must start with `gs://`
    Args:
        from_location (str): the directory to where the index.txt file resides. Prefix GSC buckets with gs://.
        task_index (number): the index of the current cloud run task
        task_count (number): the total number of cloud run tasks
        total_size (number): the total number of files to process

    Returns:
        list(str): a list of uris from the bucket based on index text file
    """
    index = get_index(from_location)

    if index is None:
        return []

    task_index = int(task_index)
    task_count = int(task_count)
    total_size = int(total_size)

    first_index, last_index = get_first_and_last_index(task_index, task_count, total_size)

    file_list = []

    with index.open("r", encoding="utf-8") as data:
        file_list = list(islice(data, first_index, last_index))

    logging.info("task number %i will work on file indices from %i to %i", task_index, first_index, last_index)

    return file_list


def generate_remaining_index(full_index_location, processed_index_location, save_location):
    """reads file names from the `from_location` and optionally saves the list to the `save_location` as an index.txt
    file. Cloud storage buckets must start with `gs://`
    Args:
        full_index_location (str): the location from which to read the full index. Prefix GSC buckets with gs://.
        processed_index_location (str): the location from which to read the already-processed index.
                                        Prefix GSC buckets with gs://.
        save_location (str): the directory to save the list of files to. An index.txt file will be created within this
                             directory
    Returns:
        list(str): a list of file names
    """

    #: Get all files from the full index
    full_index = get_index(full_index_location)

    if full_index is None:
        return []

    with full_index.open() as data:
        all_files = {l.strip() for l in data.readlines()}

    logging.info("total number of files %i", len(all_files))

    #: Get already-processed files from processed index
    processed_index = get_index(processed_index_location)

    if processed_index is None:
        return []

    with processed_index.open() as data:
        processed_files = {l.strip() for l in data.readlines()}

    logging.info("number of already-processed files %i", len(processed_files))

    #: Get the difference to determine what remaining files need to be processed
    remaining_files = all_files - processed_files
    logging.info("number of remaining files to process %i", len(remaining_files))

    if save_location is None:
        return remaining_files

    if save_location.startswith("gs://"):
        bucket = STORAGE_CLIENT.bucket(save_location[5:])
        blob = bucket.blob("remaining_index.txt")

        with BytesIO() as data:
            for item in remaining_files:
                data.write(str(item).encode("utf-8") + b"\n")

            blob.upload_from_string(data.getvalue())
    else:
        save_location = Path(save_location)

        if not save_location.exists():
            logging.warning("save location %s does not exists", save_location)

            return remaining_files

        with save_location.joinpath("remaining_index.txt").open("w", encoding="utf-8", newline="") as output:
            for item in remaining_files:
                output.write(str(item) + "\n")

    return remaining_files


def convert_pdf_to_jpg_bytes(pdf_as_bytes, object_name):
    """convert pdf to jpg images

    Args:
        pdf_as_bytes: a pdf as bytes

    Returns:
        tuple(list, number): A tuple of a list of images and the count of images
    """
    dpi = 300
    images = []
    messages = ""

    try:
        images = convert_from_bytes(pdf_as_bytes, dpi)
    except (TypeError, PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError, DecompressionBombError) as error:
        logging.error("error in %s, %s", object_name, error, exc_info=True)
        messages = error

    count = len(images)

    def convert_to_bytes(image):
        with BytesIO() as byte_array:
            image.save(byte_array, format="JPEG")

            return byte_array.getvalue()

    images = (convert_to_bytes(image) for image in images if image is not None)

    return (images, count, messages)


def get_circles_from_image_bytes(byte_img, output_path, file_name):
    """detect circles in an image (bytes) and export them as a list of cropped images

    Args:
        byte_img (bytes): The image to detect circles in
        output_path (Path): The output directory for cropped images of detected circles to be stored
        file_name (str): The name of the file to be stored
    Returns:
        list: a list of cv2 images
    """

    #: read in image from bytes
    img = None
    try:
        img = cv2.imdecode(np.frombuffer(byte_img, dtype=np.uint8), 1)  # 1 means flags=cv2.IMREAD_COLOR
    except Exception as ex:
        logging.error("unable to read image from bytes: %s, %s", file_name, ex)

        return []

    if img is None:
        logging.error("unable to read image from bytes: %s", file_name)

        return []

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.blur(gray, (5, 5))

    #: to calculate circle radius, get input image size
    [height, width, _] = img.shape

    #: original multiplier of 0.01, bigger seems to work better (0.025)
    multipliers = [
        [0.010, 12],
        [0.035, 12],
        [0.015, 12],
        [0.0325, 12],
        [0.0175, 12],
        [0.025, 10],
    ]

    i = 0
    count_down = len(multipliers)
    circle_count = 0
    detected_circles = None
    inset = 0

    while (circle_count > 100 or circle_count == 0) and count_down > 0:
        i += 1

        [ratio_multiplier, fudge_value] = multipliers[count_down - 1]

        min_rad = max(math.ceil(ratio_multiplier * height) - fudge_value, 15)
        max_rad = max(math.ceil(ratio_multiplier * height) + fudge_value, 30)

        #: original inset multiplier of 0.075, bigger seems to work better (0.1)
        inset = int(0.1 * max_rad)

        #: apply Hough transform on the blurred image.
        detected_circles = cv2.HoughCircles(
            image=gray_blur,
            method=cv2.HOUGH_GRADIENT,
            dp=1,
            minDist=min_rad,  #: space out circles to prevent multiple detections on the same object
            param1=50,
            param2=50,  #: increased from 30 to 50 to weed out some false circles (seems to work well)
            minRadius=min_rad,
            maxRadius=max_rad,
        )

        if detected_circles is None:
            circle_count = 0
        else:
            circle_count = len(detected_circles[0])

        logging.info(
            "run: %i found %i circles %s",
            i,
            circle_count,
            {
                "multiplier": ratio_multiplier,
                "fudge": fudge_value,
                "diameter": f"{min_rad}-{max_rad}",
                "inset": inset,
                "dimensions": f"{height}x{width}",
            },
        )

        count_down -= 1

    logging.info("final circles count: %i", circle_count)

    return export_circles_from_image(
        detected_circles,
        output_path,
        file_name,
        img,
        height,
        width,
        inset,
    )


def convert_to_cv2_image(image):
    """convert image (bytes) to a cv2 image object

    Args:
        image (bytes): The image (bytes) to convert

    Returns:
        cv2.Image: A cv2 image object
    """
    return cv2.imdecode(np.frombuffer(image, dtype=np.uint8), 1)  # 1 means flags=cv2.IMREAD_COLOR


def export_circles_from_image(circles, out_dir, file_name, cv2_image, height, width, inset_distance):
    """export detected circles from an image as jpegs to the out_dir

        Args:
            circles (array): Circle locations returned from cv2.HoughCircles algorithm
            out_dir (Path): The output directory for cropped images of detected circles
            file_name (str): The name of the image file
            cv2_image (numpy.ndarray): The image as a numpy array
            height (number): The height of original image
            width (number): The width of original image
            inset_distance (number): The inset distance in pixels to aid image cropping

    Returns:
        list: a list of cv2 images
    """
    if circles is None:
        logging.info("no circles detected for %s", file_name)

        return []

    #: round the values to the nearest integer
    circles = np.uint16(np.around(circles))

    color = (255, 255, 255)
    thickness = -1

    if out_dir:
        if not out_dir.exists():
            out_dir.mkdir(parents=True)

    masked_images = []

    for i, data in enumerate(circles[0, :]):  # type: ignore
        # #: prepare a black canvas on which to draw circles
        canvas = np.zeros((height, width))
        #: draw a white circle on the canvas where detected:
        center_x = data[0]
        center_y = data[1]

        radius = data[2] - inset_distance  #: inset the radius by number of pixels to remove the circle and noise

        cv2.circle(canvas, (center_x, center_y), radius, color, thickness)

        #: create a copy of the input (3-band image) and mask input to white from the canvas:
        image_copy = cv2_image.copy()
        image_copy[canvas == 0] = (255, 255, 255)

        #: crop image to the roi:
        crop_x = min(max(center_x - radius - 20, 0), width)
        crop_y = min(max(center_y - radius - 20, 0), height)
        crop_height = 2 * radius + 40
        crop_width = 2 * radius + 40

        masked_image = image_copy[crop_y : crop_y + crop_height, crop_x : crop_x + crop_width]

        if out_dir:
            original_basename = Path(file_name).stem
            out_file = out_dir / f"{original_basename}_{i}.jpg"
            cv2.imwrite(str(out_file), masked_image)

        masked_images.append(masked_image)

    return masked_images


def upload_results(data, bucket_name, out_name, job_name):
    """upload results dataframe to a GCP bucket as a gzip file

    Args:
        data (list): a list containing the results for the task (a list of lists. the first index being the file name
                     and the second being the text found)
        bucket_name (str): the name of the destination bucket
        out_name (str): the name of the gzip file

    Returns:
        nothing
    """
    file_name = f"{job_name}/{out_name}.gz"
    logging.info("uploading %s to %s/%s", out_name, bucket_name, file_name)

    bucket = STORAGE_CLIENT.bucket(bucket_name)
    new_blob = bucket.blob(file_name)

    frame = pd.DataFrame(data, columns=["file_name", "text"])

    with BytesIO() as parquet:
        frame.to_parquet(parquet, compression="gzip")

        new_blob.upload_from_string(parquet.getvalue(), content_type="application/gzip")


def format_time(seconds):
    """seconds: number
    returns a human-friendly string describing the amount of time
    """
    minute = 60.00
    hour = 60.00 * minute

    if seconds < 30:
        return f"{int(seconds * 1000)} ms"

    if seconds < 90:
        return f"{round(seconds, 2)} seconds"

    if seconds < 90 * minute:
        return f"{round(seconds / minute, 2)} minutes"

    return f"{round(seconds / hour, 2)} hours"


def download_run(bucket, run_name):
    """download a runs worth of results from a GCP bucket

    Args:
        bucket (str): the name of the bucket
        run_name (str): the name of the run

    Returns:
        str: the location of the files
    """
    bucket = STORAGE_CLIENT.bucket(bucket)
    blobs = bucket.list_blobs(prefix=run_name)
    location = Path(__file__).parent / "data"

    if not location.joinpath(run_name).exists():
        location.joinpath(run_name).mkdir(parents=True)

    for blob in blobs:
        if blob.name.endswith(".gz"):
            blob.download_to_filename(location / blob.name)

    return location.joinpath(run_name)


def summarize_run(folder, run_name):
    """summarize the results of a run

    Args:
        folder (str): the name of the folder containing the merged results
        run_name (str): the name of the output file

    Returns:
        nothing
    """
    logging.info("summarizing %s", run_name)

    folder = Path(folder) / run_name


def build_mosaic_image(images, object_name, out_dir):
    """build a mosaic image from a list of cv2 images

    Args:
        images (list): list of cv2 images to mosaic together
        object_name (str): the name of the image object (original filename)
        out_dir (Path): location to save the result

    Returns:
        mosaic_image (np.ndarray): composite mosaic of smaller images
    """
    if images is None or len(images) == 0:
        logging.info("no images to mosaic for %s", object_name)

        return np.array(None)

    object_path = Path(object_name)
    max_dim = 0
    buffer = 5

    #: Loop through all images to get dimensions, save largest dimension
    all_widths = []
    all_heights = []
    for img in images:
        all_widths.append(img.shape[1])
        all_heights.append(img.shape[0])

    max_dim = max([max(all_widths), max(all_heights)])

    #: Set up parameters for mosaic, calculate number of cols/rows
    number_images = len(images)
    number_columns = math.floor(math.sqrt(number_images))
    number_rows = math.ceil(number_images / number_columns)

    #: Build mosaic image with white background
    tile_width = max_dim + 2 * buffer
    total_height = tile_width * number_rows
    total_width = tile_width * number_columns

    logging.info(
        "mosaicking %i images into %i column by %i row grid, %s",
        number_images,
        number_columns,
        number_rows,
        {"square pixels": tile_width, "file name": object_name},
    )

    mosaic_image = np.zeros((total_height, total_width, 3), dtype=np.uint8)
    mosaic_image[:, :] = (255, 255, 255)

    if total_height * total_width > 40_000_000:
        logging.error('mosaic image size is too large: "%s"', object_name)

        return np.array(None)

    i = 0
    for img in images:
        #: Resize all images by inserting them into the same template tile size
        [img_height, img_width, _] = img.shape

        buffered_image = np.zeros((tile_width, tile_width, 3), np.uint8)
        buffered_image[:, :] = (255, 255, 255)
        buffered_image[buffer : buffer + img_height, buffer : buffer + img_width] = img.copy()

        #: Add buffered image into the mosaic
        row_start = (math.floor(i / number_columns)) * tile_width
        col_start = (i % number_columns) * tile_width
        mosaic_image[row_start : row_start + tile_width, col_start : col_start + tile_width] = buffered_image

        i += 1

    if out_dir:
        if not out_dir.exists():
            out_dir.mkdir(parents=True)

        mosaic_outfile = out_dir / f"{object_path.stem}.jpg"
        logging.info("saving to %s", mosaic_outfile)
        cv2.imwrite(str(mosaic_outfile), mosaic_image)

    else:
        return mosaic_image


def upload_mosaic(image, bucket_name, object_name, job_name):
    """upload mosaic image to a GCP bucket as a jpeg mime type

    Args:
        image (np.array): the mosaic image bytes
        bucket_name (str): the name of the destination bucket
        object_name (str): the name of the image object (original filename)

    Returns:
        bool: True if successful, False otherwise
    """
    #: Upload mosaic image to GCP bucket
    if image is None or not image.any():
        logging.info('no mosaic image created or uploaded: "%s"', object_name)

        return False

    file_name = f"{job_name}/mosaics/{object_name}"
    logging.info("uploading %s to %s/%s", object_name, bucket_name, file_name)

    bucket = STORAGE_CLIENT.bucket(bucket_name)
    new_blob = bucket.blob(str(file_name))

    is_success, buffer = cv2.imencode(".jpg", image)

    if not is_success:
        logging.error("unable to encode image: %s", object_name)

    new_blob.upload_from_string(buffer.tobytes(), content_type="image/jpeg")

    return True
