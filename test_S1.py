import os.path as osp
import basicsr
import hi_diff
# python test_S1.py -opt options/test/test_S1.yml 
if __name__ == '__main__':
    root_path = osp.abspath(osp.join(__file__, osp.pardir))
    basicsr.test_pipeline(root_path)
