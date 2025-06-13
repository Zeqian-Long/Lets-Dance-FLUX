# python edit.py  --source_prompt "A large brown bear walking on rocky ground inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
#                 --target_prompt "A tall brown horse with a long tail walking on rocky ground inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
#                 --guidance 2 \
#                 --source_img_dir 'examples/source/bear.jpg' \
#                 --num_steps 15 --offload \
#                 --inject 4 \
#                 --name 'flux-dev'  \
#                 --output_dir 'examples/edit-result/bear' \
#                 --controlnet_type 'single' \


# python edit.py  --source_prompt "A large brown bear walking on the ground inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
#                 --target_prompt "Empty ground inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
#                 --guidance 2 \
#                 --source_img_dir 'examples/source/bear.jpg' \
#                 --num_steps 15 --offload \
#                 --inject 4 \
#                 --name 'flux-dev'  \
#                 --output_dir 'examples/edit-result/bear' 


python edit.py  --source_prompt "A large brown bear walking inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
                --target_prompt "A powerful dark bull walking inside a zoo enclosure, with a stone wall in the background and some plants at the edge of the frame." \
                --guidance 2 \
                --source_img_dir 'examples/source/bear.jpg' \
                --num_steps 15 --offload \
                --inject 4 \
                --name 'flux-dev'  \
                --output_dir 'examples/edit-result/bear' \
                --controlnet_type 'single' \




