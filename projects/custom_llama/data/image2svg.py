import subprocess
from modelzipper.tutils import *
import subprocess
import concurrent.futures
import vtracer
from tqdm import tqdm
from threading import Lock
from multiprocessing import Pool, cpu_count
from tqdm import tqdm
import os

def process_image(args):
    image_path, output_dir = args
    output_path = f"{output_dir}/{os.path.splitext(os.path.basename(image_path))[0]}.svg"
    convert_image_to_svg(image_path, output_path)
    return image_path 

def mp_process_images(image_paths, output_dir, num_workers=None):
    if num_workers is None:
        num_workers = cpu_count()

    progress_bar = tqdm(total=len(image_paths), desc='Processing')

    with Pool(num_workers) as pool:
        arguments = [(image_path, output_dir) for image_path in image_paths]
        for _ in pool.imap(process_image, arguments):
            progress_bar.update(1)

    progress_bar.close()


def process_images(image_paths, output_dir):
    for image_path in tqdm(image_paths, desc='Processing'):
        convert_image_to_svg(image_path, f"{output_dir}/{os.path.splitext(os.path.basename(image_path))[0]}.svg")


def convert_image_to_svg(image_path, output_path):
    vtracer.convert_image_to_svg_py(
        image_path,
        output_path,
        colormode = 'binary',        # ["color"] or "binary"
        hierarchical = 'stacked',   # ["stacked"] or "cutout"
        mode = 'spline',            # ["spline"] "polygon", or "none"
        filter_speckle = 4,         # default: 4
        color_precision = 6,        # default: 6
        layer_difference = 16,      # default: 16
        corner_threshold = 60,      # default: 60
        length_threshold = 4.0,     # in [3.5, 10] default: 4.0
        max_iterations = 10,        # default: 10
        splice_threshold = 45,      # default: 45
        path_precision = 3          # default: 8
    )


def process_scienceqa(image_file_dir, meta_file, caption_file):
    meta_data = auto_read_data(meta_file)
    caption_data = auto_read_data(caption_file)
    image_folders, saved_res = [], []
    subdirectories = [name for name in os.listdir(image_file_dir) if os.path.isdir(os.path.join(image_file_dir, name))]

    for subdirectory in tqdm(subdirectories, desc='Processing'):
        subdir_path = os.path.join(image_file_dir, subdirectory)
        if 'image.png' in os.listdir(subdir_path):
            image_folders.append(subdir_path)
            image_path = os.path.join(subdir_path, 'image.png')
            caption = caption_data[subdirectory]    
            saved_res.append({
                'image_path': image_path,
                'caption': caption
            })

    auto_save_data(saved_res, os.path.join(SAVE_DIR, 'scienceqa_image.jsonl'))


def process_mscoco(image_dir, meta_file):
    saved_res = []
    meta_data = auto_read_data(meta_file)
    for item in tqdm(meta_data, desc='Processing'):
        image_path = os.path.join(image_dir, item['image_name'])
        saved_res.append({
            'image_path': image_path,
            'caption': item['captions']
        })
    auto_save_data(saved_res, '/zecheng2/svg/mscoco/mscoco_val.jsonl')
    


if __name__ == '__main__':

    ## ScienceQA Dataset
    # IMAGE_FILE_DIR = "/zecheng2/dataset/ScienceQA/tutorial/train"
    # META_FILE = "/zecheng2/dataset/ScienceQA/tutorial/problems.json"
    # CAPTION_FILE = "/zecheng2/dataset/ScienceQA/tutorial/captions.json"
    # SAVE_DIR = "/zecheng2/dataset/ScienceQA/"
    # process_scienceqa(IMAGE_FILE_DIR, META_FILE, CAPTION_FILE)

    ## MSCOCO Dataset
    # IMAGE_DIR = "/data/G/dataset/mscoco/val2017"
    # META_FILE = "/data/G/dataset/mscoco/val_data.json"
    # res = process_mscoco(IMAGE_DIR, META_FILE)


    ## Test
    META_FILE = "/zecheng2/svg/mscoco/mscoco_train.jsonl"
    meta_data = auto_read_data(META_FILE)
    output_dir = "/zecheng2/svg/mscoco/convert_svg_train"
    
    image_paths = [item['image_path'] for item in meta_data]
    mp_process_images(image_paths, output_dir, num_workers=16)