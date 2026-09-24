FROM ros:humble-ros-base-jammy

ENV DEBIAN_FRONTEND=noninteractive
ENV FQPLANNER_ROOT=/opt/fqplanner_nav

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-colcon-common-extensions \
    python3-pip \
    curl \
    iproute2 \
    net-tools \
    ros-humble-navigation2 \
    ros-humble-nav2-bringup \
    ros-humble-nav2-msgs \
    ros-humble-tf2-ros \
    ros-humble-tf2-geometry-msgs \
    ros-humble-geometry-msgs \
    ros-humble-nav-msgs \
    ros-humble-sensor-msgs \
    ros-humble-std-msgs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/fqplanner_nav

COPY ros2_ws ./ros2_ws

RUN . /opt/ros/humble/setup.sh \
    && cd /opt/fqplanner_nav/ros2_ws \
    && colcon build --symlink-install --packages-select fqplanner_nav_bridge

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
